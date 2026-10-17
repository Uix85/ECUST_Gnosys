"""端口清理的解析单测（不真正结束任何进程）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from api import port_cleanup  # noqa: E402


class WindowsNetstatTest(unittest.TestCase):
    def test_extracts_only_listeners_on_requested_port(self):
        output = """  TCP    127.0.0.1:8123         0.0.0.0:0              LISTENING       1234
  TCP    [::1]:8123              [::]:0                 LISTENING       5678
  TCP    127.0.0.1:8123          127.0.0.1:50123        ESTABLISHED     1234
  TCP    127.0.0.1:8124          0.0.0.0:0              LISTENING       9999
  TCP    0.0.0.0:8123            0.0.0.0:0              LISTENING       1234
"""
        self.assertEqual(port_cleanup._windows_listening_pids(output, 8123), [1234, 5678])

    def test_empty_when_no_listener(self):
        self.assertEqual(port_cleanup._windows_listening_pids("", 8123), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
