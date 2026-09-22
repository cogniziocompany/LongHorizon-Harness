# Fleet Reporter Payload Bound Verification Plan

This document outlines the steps to verify that the fleet reporter payload bounding fix is working correctly after deployment to CT110.

## Pre-deployment Checks

1. Verify the fix is present in the deployed code:
   - Check that `src/lh_harness/fleet/reporter.py` contains the `MAX_RUNS_PER_HEARTBEAT = 500` constant
   - Verify the truncation logic exists in the `queue_heartbeat` method
   - Confirm the informational log message is present

## Post-deployment Verification Steps

### 1. Basic Reporter Health

Check that the fleet reporter is enabled and configured:
```bash
# On CT110, check the lh-harness service status
systemctl status lh-harness

# Verify environment variables are set
systemctl show lh-harness -p Environment | grep LH_HARNESS_FLEET
```

### 2. Heartbeat Endpoint Verification

Verify that the reporter is successfully sending heartbeats:
```bash
# Check the fleet admin API for the node
curl -H "Authorization: Bearer $LH_HARNESS_WEB_TOKEN" \
  https://fleet.easybutt0n.ai/api/fleet/nodes | grep ct110
```

### 3. Payload Size Verification

To verify the payload bounding is working:

#### Option A: Check logs for truncation messages
```bash
# Look for the truncation log message in the lh-harness logs
journalctl -u lh-harness | grep "fleet reporter heartbeat: truncating runs"
```

If you see messages like `fleet reporter heartbeat: truncating runs from X to 500 most recent`, the bounding is active.

#### Option B: Measure actual heartbeat payload size
1. Execute the measurement script locally to establish baseline:
   ```bash
   python3 /tmp/measure_real_heartbeat2.py
   ```

2. Compare the output to ensure the payload size is reasonable (<10 MB)

#### Option C: Verify via fleet admin metrics (if available)
Check if the fleet admin provides payload size metrics or if we can infer from successful 200 responses vs 413 errors.

### 4. Functional Verification

Ensure that the reporter still sends all required data:
- Node information (name, version, labels)
- Capacity information (active, cap)
- Queue length
- Run data (limited to 500 most recent)

### 5. Regression Testing

Verify that normal operation continues when run count is below the threshold:
- Nodes with <500 runs should see no truncation in logs
- Heartbeat should include all runs when count is low

## Success Criteria

1. **No 413 Errors**: The fleet admin should no longer return 413 Payload Too Large errors for CT110 heartbeats
2. **Successful Heartbeats**: The `/api/fleet/nodes` endpoint should show `ct110` with `fleet_ever_succeeded: true`
3. **Proper Bounding**: When the node has >500 runs, heartbeat logs show truncation messages
4. **No Regressions**: All existing fleet reporter functionality continues to work
5. **Informational Logging**: Truncation events are logged at INFO level for operational visibility

## Troubleshooting

If you see continued 413 errors:
1. Verify the reporter process has been restarted after deploy
2. Check that the fix is actually deployed (check the running code)
3. Verify the node actually has >500 runs (the bounding only applies when exceeding the limit)
4. Check fleet admin logs for any other size limits

If truncation is not occurring when expected:
1. Verify the node actually has >500 runs
2. Check that the runs have proper `mtime` (updated_at) values for sorting
3. Ensure the reporter is using the updated code (check version)

## References

- Code changes: `src/lh_harness/fleet/reporter.py`
- Test: `tests/fleet/test_reporter.py::test_heartbeat_truncates_runs`
- Documentation: `docs/fleet-reporting.md` (updated payload bounding section)