from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from lh_harness.safe_subprocess import (
    NamespaceProbeOutcome,
    _user_namespace_actually_isolating,
)


@pytest.mark.parametrize(
    "code,output",
    [
        # Launcher missing (rc=127), no marker: LAUNCH_FAILED.
        (127, ""),
        # Launcher exited nonzero without the marker: LAUNCH_FAILED (the
        # kernel might have refused to create the namespace itself with
        # ``EPERM``).
        (1, ""),
        # Marker printed but the script claimed success (rc=0): NOT_ISOLATING
        # trap (the kernel check was ineffective because the inner uid
        # mapped back to the outer uid). We must NOT treat this as
        # isolation available.
        (0, "LH_PERMISSION_DENIED"),
    ],
)
def test_probe_failures_distinguish_from_permission_denial(code, output):
    with patch("lh_harness.safe_subprocess._subprocess.run", return_value=CompletedProcess([], code, output, "")):
        outcome = _user_namespace_actually_isolating()
    assert outcome is NamespaceProbeOutcome.LAUNCH_FAILED or outcome is NamespaceProbeOutcome.NOT_ISOLATING
    assert outcome is not NamespaceProbeOutcome.ISOLATING


def test_permission_denial_marker_with_exit_77_is_isolating():
    """Successful entry + EACCES on the inner write is the only
    ISOLATING outcome. Any nonzero error that is not this marker is
    not isolation; the kernel check is not authoritatively real.
    """
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=CompletedProcess([], 77, "LH_PERMISSION_DENIED\n", ""),
    ):
        outcome = _user_namespace_actually_isolating()
    assert outcome is NamespaceProbeOutcome.ISOLATING


def test_probe_error_marker_is_launch_failed_not_isolating():
    """The probe prints ``LH_PROBE_ERROR`` for any non-permission
    exception the inner script encounters (FileNotFoundError,
    IsADirectoryError, etc.). This is a probe-side launch failure,
    NOT a permission denial, so the kernel check cannot be claimed.
    """
    with patch(
        "lh_harness.safe_subprocess._subprocess.run",
        return_value=CompletedProcess([], 2, "LH_PROBE_ERROR FileNotFoundError\n", ""),
    ):
        outcome = _user_namespace_actually_isolating()
    assert outcome is NamespaceProbeOutcome.LAUNCH_FAILED
