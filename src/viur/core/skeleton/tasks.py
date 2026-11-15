import copy
import datetime
import logging
import typing as t
import logics
import time
import uuid

from viur.core import (
    conf,
    current,
    db,
    email,
    errors,
    tasks,
    utils,
)
from .utils import skeletonByKind, listKnownSkeletons
from .relskel import RelSkel

from ..bones.base import BaseBone
from ..bones.code import LogicsBone
from ..bones.record import RecordBone
from ..bones.relational import RelationalBone, RelationalConsistency, RelationalUpdateLevel
from ..bones.select import SelectBone
from ..bones.string import StringBone


PENDING_KIND = "viur-relations-pending"
"""Kind of the markers for update_relations chains that are not done yet, see :func:`pending_marker`."""

PENDING_STALE_AFTER = 15 * 60
"""Seconds without a sign of life after which a marker's chain is considered lost and queued again."""

PENDING_MAX_ATTEMPTS = 5
"""How often a lost chain is queued again before it is reported instead."""


def pending_marker(key: db.Key, changed_bones: t.Optional[list[str]]) -> db.Entity:
    """
        A marker for the ``update_relations`` of ``key`` that still has to run.

        The datastore and Cloud Tasks share no transaction: a write can commit and the creation of its task fail, or
        the task can run out of attempts, and no entity referencing ``key`` would ever be refreshed.
        :meth:`viur.core.skeleton.Skeleton.write` therefore puts this marker inside its transaction. The task chain
        keeps it alive while it runs and deletes it when it is done; :func:`requeue_pending_relation_updates` queues
        what is left again.
    """
    now = time.time()
    marker = db.Entity(db.Key(PENDING_KIND, uuid.uuid4().hex))
    marker["target"] = key
    marker["changed_bones"] = changed_bones
    marker["created"] = marker["queued"] = now
    marker["attempts"] = 0
    return marker


@tasks.PeriodicTask(interval=datetime.timedelta(minutes=10))
def requeue_pending_relation_updates() -> None:
    """
        Queues every ``update_relations`` again whose marker shows no sign of life, see :func:`pending_marker`.

        A chain that was lost :data:`PENDING_MAX_ATTEMPTS` times is not queued again but reported with a critical
        log message on every run, until someone resolves it (and deletes the marker).
    """
    now = time.time()
    for marker in db.Query(PENDING_KIND).filter("queued <", now - PENDING_STALE_AFTER).iter():
        if marker["attempts"] >= PENDING_MAX_ATTEMPTS:
            logging.critical(
                f"""update_relations for {marker["target"]!r} failed {marker["attempts"]} times and is not """
                f"""queued again; entities referencing it may hold outdated values ({marker.key!r})"""
            )
            continue

        marker["attempts"] += 1
        marker["queued"] = now
        db.put(marker)
        logging.warning(f"""Queueing lost update_relations for {marker["target"]!r} again ({marker["attempts"]=})""")
        update_relations(marker["target"], changed_bones=marker["changed_bones"], pending=marker.key.id_or_name)


@tasks.CallDeferred
def update_relations(
    key: db.Key,
    *,
    min_change_time: t.Optional[float] = None,
    changed_bones: t.Optional[t.Iterable[str] | str] = (),
    cursor: t.Optional[str] = None,
    total: int = 0,
    pending: t.Optional[str] = None,
    **kwargs
):
    """
        This function updates Entities, which may have a copy of values from another entity which has been recently
        edited (updated). In ViUR, relations are implemented by copying the values from the referenced entity into the
        entity that's referencing them. This allows ViUR to run queries over properties of referenced entities and
        prevents additional db.Get's to these referenced entities if the main entity is read. However, this forces
        us to track changes made to entities as we might have to update these mirrored values.     This is the deferred
        call from meth:`viur.core.skeleton.Skeleton.write()` after an update (edit) on one Entity to do exactly that.

        :param key: The database-key of the entity that has been edited
        :param min_change_time: The timestamp on which the edit occurred. As we run deferred, and the entity might have
            been edited multiple times before we get acutally called, we can ignore entities that have been updated
            in the meantime as they're  already up-to-date
        :param changed_bones: If set, we'll update only entites that have a copy of that bones. Relations mirror only
            key and name by default, so we don't have to update these if only another bone has been changed.
        :param cursor: The database cursor for the current request as we only process five entities at once and then
            defer again.
        :param pending: Name of the marker of this chain, see :func:`pending_marker`.
    """
    # TODO: Remove in VIUR4
    for _dep, _new in {
        "changedBone": "changed_bones",
        "minChangeTime": "min_change_time",
        "destKey": "key",
    }.items():
        if _dep in kwargs:
            logging.warning(f"{_dep!r} parameter is deprecated, please use {_new!r} instead",)
            locals()[_new] = kwargs.pop(_dep)

    if min_change_time is None:
        # Not later than now: refreshing a source rewrites its relations with viur_delayed_update_tag = now, behind
        # the cursor of this tag-ordered query, and a cutoff in the future let the next page find them again.
        min_change_time = time.time()

    changed_bones = utils.ensure_iterable(changed_bones)

    if not cursor:
        logging.debug(f"update_relations {key=} {min_change_time=} {changed_bones=}")

    if request_data := current.request_data.get():
        request_data["__update_relations_bones"] = changed_bones

    query = db.Query("viur-relations") \
        .filter("dest.__key__ =", key) \
        .filter("viur_delayed_update_tag <", min_change_time) \
        .filter("viur_relational_updateLevel =", RelationalUpdateLevel.Always.value)

    if changed_bones:
        query.filter("viur_foreign_keys IN", changed_bones)

    query.setCursor(cursor)

    # A single entity can reference the same destination more than once (e.g. a list
    # bone holding the same reference several times), yielding one relation per
    # occurrence. Refreshing that entity once is enough -- doing it per relation only
    # stacks transactions on the very same entity and can exceed the request deadline.
    seen_src_keys: set[db.Key] = set()

    for src_rel in query.run():
        src_key = src_rel["src"].key
        if src_key in seen_src_keys:
            continue
        seen_src_keys.add(src_key)

        try:
            skel = skeletonByKind(src_rel["viur_src_kind"])()
        except AssertionError:
            logging.info(f"""Ignoring {src_rel.key!r} which refers to unknown kind {src_rel["viur_src_kind"]!r}""")
            continue

        try:
            # Another task of the same or a later change, or a write of the source itself, may have brought it up
            # to date already: then there is nothing to write.
            skel.patch(lambda skel: skel.refresh(), key=src_key, update_relations=False, skip_unchanged=True)
        except ValueError:
            logging.warning(f"Cannot update stale reference to {src_key!r} referenced by {src_rel.key!r}")
            continue

        total += 1

    if next_cursor := query.getCursor():
        if pending and (marker := db.get(db.Key(PENDING_KIND, pending))):
            marker["queued"] = time.time()  # a sign of life: this chain is running
            db.put(marker)

        update_relations(
            key=key,
            min_change_time=min_change_time,
            changed_bones=changed_bones,
            cursor=next_cursor,
            total=total,
            pending=pending,
        )
    else:
        if pending:
            db.delete(db.Key(PENDING_KIND, pending))

        logging.debug(f"update_relations finished with {total=} on {key=} {min_change_time=} {changed_bones=}")


def outdated_relation_bones(skel: "SkeletonInstance") -> list[str]:
    """
        The bones of a skeleton, read from the datastore, whose stored copies of other entities differ from what these
        entities hold now (relations with ``updateLevel`` ``Always``, also inside records and using skeletons).

        Nothing is written. The values of ``skel`` are brought up to date on the way, so a following
        ``skel.write(update_relations=False)`` repairs the entity.
    """
    stored = copy.deepcopy(dict(skel.dbEntity))
    outdated = []

    for name, bone in skel.items():
        if type(bone).refresh_copies is BaseBone.refresh_copies or not skel[name]:
            continue  # holds no copies

        bone.refresh_copies(skel, name)
        bone.serialize(skel, name, True)  # as write does, to compare what would be stored

        if skel.dbEntity.get(name) != stored.get(name):
            outdated.append(name)

    return outdated


class SkelIterTask(tasks.QueryIter):
    """
    Iterates the skeletons of a query, and additionally checks a Logics expression.
    When the skeleton is valid, it performs the action `data["action"]` on each entry.
    """

    @classmethod
    def handleEntry(cls, skel, data):
        data["total"] += 1

        if logics.Logics(data["condition"]).run(skel):
            data["count"] += 1

            match data["action"]:
                case "refresh":
                    skel.refresh()
                    if skel["key"]:
                        skel.write(update_relations=False)

                case "delete":
                    skel.delete()

                case "check_relations" | "repair_relations":
                    if outdated := outdated_relation_bones(skel):
                        data["outdated"] += 1
                        logging.warning(f"""{skel["key"]!r} holds outdated copies in {outdated}""")

                        if data["action"] == "repair_relations":
                            skel.write(update_relations=False)

                case other:
                    assert other == "count"

    @classmethod
    def handleError(cls, skel, data, exception) -> bool:
        logging.exception(exception)

        try:
            logging.debug(f"{skel=!r}")
        except Exception:  # noqa
            logging.warning("Failed to dump skel")
            logging.debug(f"{skel.dbEntity=}")

        data["error"] += 1
        return True

    @classmethod
    def handleFinish(cls, total, data):
        super().handleFinish(total, data)

        if not data["notify"]:
            return

        txt = (
            f"{conf.instance.project_id}: {data['action']!s} finished for {data['kind']!r}: "
            f"{data['count']} of {data['total']}\n"
            f"ViUR {data['action']!s}ed {data['count']} skeletons with condition <code>{data['condition']}</code> on a "
            f"total of {data['total']} ({data['error']} errored) of kind {data['kind']}.\n"
        )

        if "relations" in data["action"]:
            txt += f"{data['outdated']} of them held outdated copies of other entities.\n"

        try:
            email.send_email(dests=data["notify"], stringTemplate=txt, skel=None)
        except Exception as exc:  # noqa; OverQuota, whatever
            logging.exception(f'Failed to notify {data["notify"]}')


@tasks.CallableTask
class SkeletonMaintenanceTask(tasks.CallableTaskBase):
    key = "SkeletonMaintenanceTask"
    name = "Skeleton Maintenance"
    descr = "Perform filtered maintenance operations on skeletons."

    def canCall(self):
        user = current.user.get()
        return user and "root" in user["access"]

    class dataSkel(RelSkel):
        task = SelectBone(
            descr="Task",
            required=True,
            values={
                "count": "Count",
                "refresh": "Refresh (formerly: RebuildSearchIndex)",
                "delete": "Delete",
                "check_relations": "Check relations (report entries holding outdated copies)",
                "repair_relations": "Repair relations (rewrite entries holding outdated copies)",
            },
            defaultValue="refresh",
        )

        kinds = SelectBone(
            descr="Kind",
            values=listKnownSkeletons,
            required=True,
            multiple=True,
        )

        class FilterRowUsingSkel(RelSkel):
            name = StringBone(
                required=True,
            )

            op = SelectBone(
                required=True,
                values={
                    "$eq": "=",
                    "$lt": "<",
                    "$gt": ">",
                    "$lk": "like",
                },
                defaultValue=" ",
            )

            value = StringBone(
                required=True,
            )

        filters = RecordBone(
            descr="Filter",
            using=FilterRowUsingSkel,
            multiple=True,
            format="$(name)$(op)=$(value)",
        )

        condition = LogicsBone(
            descr="Condition",
            required=True,
            defaultValue="False  # fused: by default, doesn't affect anything.\n",
            params={
                "tooltip": "Enter a Logics expression here to filter entries by specific skeleton values."
            },
        )

    def execute(self, task, kinds, filters, condition):
        notify = current.user.get()["name"]

        for kind in kinds:
            q = skeletonByKind(kind)().all()

            for flt in filters:
                q.mergeExternalFilter({(flt["name"] + flt["op"]).rstrip("$eq"): flt["value"]})

            params = {
                "action": task,
                "notify": notify,
                "condition": condition,
                "kind": kind,
                "count": 0,
                "total": 0,
                "error": 0,
                "outdated": 0,
            }

            SkelIterTask.startIterOnQuery(q, params)
