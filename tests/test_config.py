import logging
import tempfile
import unittest
from pathlib import Path

from pipertv.config import TEMPLATE, read_pin, write_pin

logging.getLogger("pipertv.config").addHandler(logging.NullHandler())


class ConfigFileTests(unittest.TestCase):
    """pipertv.conf: the one setting that has to be right before the remote is."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "pipertv.conf"

    def test_no_file_means_finding_the_receiver_by_itself(self):
        self.assertEqual(read_pin(self.path), "auto")

    def test_choosing_a_pin_writes_the_file_with_its_explanation(self):
        write_pin(18, self.path)
        text = self.path.read_text()
        self.assertEqual(text, TEMPLATE.format(pin=18))
        self.assertIn("physical pin 11", text)
        self.assertEqual(read_pin(self.path), 18)

    def test_changing_it_again_keeps_every_other_line(self):
        self.path.write_text("# mine\n[ir]\n# where it is\npin = 17\n\n[later]\nx = 1\n")
        write_pin(18, self.path)
        self.assertEqual(self.path.read_text(),
                         "# mine\n[ir]\n# where it is\npin = 18\n\n[later]\nx = 1\n")

    def test_a_pin_written_by_hand_is_read_as_written(self):
        for written, pin in (("pin = GPIO22", 22), ("pin=5", 5), ("pin = AUTO", "auto")):
            with self.subTest(written=written):
                self.path.write_text(f"[ir]\n{written}\n")
                self.assertEqual(read_pin(self.path), pin)

    def test_something_unusable_falls_back_and_says_so(self):
        # The remote is how it gets fixed, so Piper must still start.
        self.path.write_text("[ir]\npin = 40\n")
        with self.assertLogs("pipertv.config", level="WARNING") as said:
            self.assertEqual(read_pin(self.path), "auto")
        self.assertIn("40", said.output[0])

    def test_a_commented_pin_is_not_a_setting(self):
        self.path.write_text("[ir]\n#   pin = 18     the receiver is on GPIO18\n")
        self.assertEqual(read_pin(self.path), "auto")

    def test_a_nonsense_choice_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            write_pin(99, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
