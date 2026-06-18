from unittest import mock

from abstract import ViURTestCase


class TestRecordBoneFormatGeneration(ViURTestCase):
    """Auto-generation of the ``format`` for a RecordBone when none is given (issue #1592)."""

    def test_generates_format_from_name_bone(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class AddressRelSkel(RelSkel):
            name = StringBone()
            street = StringBone()

        bone = RecordBone(using=AddressRelSkel)
        self.assertEqual("$(name)", bone.format)

    def test_generates_format_from_title_bone(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class ArticleRelSkel(RelSkel):
            title = StringBone()
            body = StringBone()

        bone = RecordBone(using=ArticleRelSkel)
        self.assertEqual("$(title)", bone.format)

    def test_generates_format_from_titel_bone(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class GermanRelSkel(RelSkel):
            titel = StringBone()
            inhalt = StringBone()

        bone = RecordBone(using=GermanRelSkel)
        self.assertEqual("$(titel)", bone.format)

    def test_name_takes_priority_over_title(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class BothRelSkel(RelSkel):
            title = StringBone()
            name = StringBone()

        bone = RecordBone(using=BothRelSkel)
        self.assertEqual("$(name)", bone.format)

    def test_fallback_concatenates_visible_bones(self):
        from viur.core.bones import NumericBone, RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class PairRelSkel(RelSkel):
            foo = StringBone()
            bar = NumericBone()

        bone = RecordBone(using=PairRelSkel)
        self.assertEqual("$(foo) $(bar)", bone.format)

    def test_fallback_excludes_invisible_bones(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class MixedRelSkel(RelSkel):
            foo = StringBone()
            secret = StringBone(visible=False)
            bar = StringBone()

        bone = RecordBone(using=MixedRelSkel)
        self.assertEqual("$(foo) $(bar)", bone.format)

    def test_generated_format_is_computed_only_once(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class NameRelSkel(RelSkel):
            name = StringBone()

        bone = RecordBone(using=NameRelSkel)
        with mock.patch.object(
            RecordBone, "_generate_format", wraps=bone._generate_format,
        ) as spy:
            self.assertEqual("$(name)", bone.format)
            self.assertEqual("$(name)", bone.format)

        spy.assert_called_once()

    def test_explicit_format_takes_precedence(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class NameRelSkel(RelSkel):
            name = StringBone()

        bone = RecordBone(using=NameRelSkel, format="$(name) (custom)")
        self.assertEqual("$(name) (custom)", bone.format)

    def test_indexed_record_bone_raises(self):
        from viur.core.bones import RecordBone, StringBone
        from viur.core.skeleton.relskel import RelSkel

        class NameRelSkel(RelSkel):
            name = StringBone()

        with self.assertRaises(NotImplementedError):
            RecordBone(using=NameRelSkel, indexed=True)
