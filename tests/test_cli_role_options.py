def test_runtime_only_role_resolves_through_alias():
    """auditor_format_repair has no CLI flags; it must fall back to the auditor chain, not KeyError."""
    import argparse
    from lh_harness import cli

    args = argparse.Namespace(agent="claude", model="m0", auditor_model="m-aud", auditor_agent=None)
    assert cli._resolve_role_option(args, "auditor_format_repair", "model") == "m-aud"
    assert cli._resolve_role_option(args, "auditor_format_repair", "agent") == "claude"
