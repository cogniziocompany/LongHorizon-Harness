from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from lh_harness.safe_subprocess import _user_namespace_actually_isolating


@pytest.mark.parametrize("code,output", [(1, ""), (127, ""), (77, ""), (0, "LH_PERMISSION_DENIED")])
def test_launcher_failure_is_not_write_isolation(code, output):
    with patch("lh_harness.safe_subprocess._subprocess.run", return_value=CompletedProcess([], code, output, "")):
        assert not _user_namespace_actually_isolating()


def test_permission_denial_requires_successfully_executed_probe_marker():
    with patch("lh_harness.safe_subprocess._subprocess.run", return_value=CompletedProcess([], 77, "LH_PERMISSION_DENIED\n", "")):
        assert _user_namespace_actually_isolating()
