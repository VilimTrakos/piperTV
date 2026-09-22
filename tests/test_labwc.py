import tempfile
import unittest
from pathlib import Path

from pipertv.labwc import ACCESSIBILITY, RULES, add_window_rules, allow_accessibility

SYSTEM_RC = """<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <theme>
    <name>PiXtrix</name>
  </theme>
  <windowRules>
    <windowRule identifier="Kodi" serverDecoration="yes" />
  </windowRules>
</openbox_config>
"""


class WindowRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.system = Path(self.temporary.name) / "rc.xml"
        self.system.write_text(SYSTEM_RC)
        self.rc = self.home / ".config/labwc/rc.xml"

    def test_a_desktop_without_its_own_file_starts_from_the_system_s(self):
        # Nothing about the desktop may change but the rules themselves,
        # whether labwc merges the two files or reads only this one.
        self.assertEqual(add_window_rules(self.home, self.system), self.rc)
        text = self.rc.read_text()
        self.assertIn("<name>PiXtrix</name>", text)
        self.assertIn('<windowRule identifier="Kodi" serverDecoration="yes" />', text)
        self.assertIn('<windowRule identifier="chrome-*" serverDecoration="no" skipTaskbar="yes"/>',
                      text)
        self.assertEqual(text.count("<windowRules>"), 1)

    def test_the_rules_go_inside_the_rules_already_there(self):
        add_window_rules(self.home, self.system)
        text = self.rc.read_text()
        self.assertLess(text.index("<windowRules>"), text.index('"chrome-*"'))
        self.assertLess(text.index('"chrome-*"'), text.index("</windowRules>"))

    def test_a_file_with_no_rules_gets_a_block_of_its_own(self):
        self.rc.parent.mkdir(parents=True)
        self.rc.write_text("<labwc_config>\n  <core><gap>4</gap></core>\n</labwc_config>\n")
        add_window_rules(self.home, self.system)
        self.assertEqual(self.rc.read_text(),
                         "<labwc_config>\n  <core><gap>4</gap></core>\n  <windowRules>\n"
                         + RULES + "  </windowRules>\n</labwc_config>\n")

    def test_rules_someone_already_wrote_are_left_alone(self):
        self.rc.parent.mkdir(parents=True)
        mine = ('<labwc_config><windowRules><windowRule identifier="chrome-*" '
                'serverDecoration="yes"/></windowRules></labwc_config>\n')
        self.rc.write_text(mine)
        self.assertIsNone(add_window_rules(self.home, self.system))
        self.assertEqual(self.rc.read_text(), mine)

    def test_adding_them_twice_changes_nothing(self):
        add_window_rules(self.home, self.system)
        once = self.rc.read_text()
        self.assertIsNone(add_window_rules(self.home, self.system))
        self.assertEqual(self.rc.read_text(), once)

    def test_a_file_it_cannot_understand_is_not_touched(self):
        self.rc.parent.mkdir(parents=True)
        self.rc.write_text("not xml at all\n")
        self.assertIsNone(add_window_rules(self.home, self.system))
        self.assertEqual(self.rc.read_text(), "not xml at all\n")


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.system = Path(self.temporary.name) / "environment"
        self.system.write_text("XKB_DEFAULT_LAYOUT=gb\n")
        self.environment = self.home / ".config/labwc/environment"

    def test_the_system_s_settings_are_kept_and_the_bus_allowed(self):
        allow_accessibility(self.home, self.system)
        lines = self.environment.read_text().splitlines()
        self.assertEqual(lines[0], "XKB_DEFAULT_LAYOUT=gb")
        self.assertEqual(lines[-1], ACCESSIBILITY)

    def test_a_contrary_value_is_replaced_not_contradicted(self):
        # GTK reads the value, not whether the name is set at all.
        self.environment.parent.mkdir(parents=True)
        self.environment.write_text("XKB_DEFAULT_LAYOUT=hr\nNO_AT_BRIDGE=1\n")
        allow_accessibility(self.home, self.system)
        self.assertEqual(self.environment.read_text(), "XKB_DEFAULT_LAYOUT=hr\nNO_AT_BRIDGE=0\n")

    def test_allowing_it_twice_changes_nothing(self):
        allow_accessibility(self.home, self.system)
        once = self.environment.read_text()
        self.assertIsNone(allow_accessibility(self.home, self.system))
        self.assertEqual(self.environment.read_text(), once)


if __name__ == "__main__":
    unittest.main()
