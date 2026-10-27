"""M0.1: policy loader and settings."""

from pennant.core.config import (
    NOTICES_DIR,
    Settings,
    config_hash,
    load_policy,
    load_settings,
    meeting_key_for,
    notice_label,
    notices,
)


def test_repo_policy_loads():
    p = load_policy()
    assert p.ned_points_threshold == 4
    assert p.count_seat_under_vote == "yes"
    assert p.aim_counts_as_listed == "yes"
    assert p.debt_only_listing_counts == "yes"
    assert p.chair_designate_counts_as_chair == "no"
    assert p.group_dedupe == "yes"


def test_policy_bare_and_quoted_yes_no(tmp_path):
    bare = tmp_path / "bare.yaml"
    bare.write_text("count_seat_under_vote: yes\ndebt_only_listing_counts: no\ngroup_dedupe: Yes\n")
    quoted = tmp_path / "quoted.yaml"
    quoted.write_text(
        'count_seat_under_vote: "yes"\ndebt_only_listing_counts: "no"\ngroup_dedupe: "yes"\n'
    )
    assert load_policy(bare) == load_policy(quoted)
    p = load_policy(bare)
    assert (p.count_seat_under_vote, p.debt_only_listing_counts) == ("yes", "no")


def test_config_hash_tracks_policy(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("aim_counts_as_listed: yes\n")
    assert config_hash(load_policy()) != config_hash(load_policy(f))
    assert config_hash(load_policy()) == config_hash(load_policy())


def test_settings_from_env_file(tmp_path, monkeypatch):
    for k in ("CH_API_KEY", "LLAMA_CLOUD_API_KEY", "ANTHROPIC_API_KEY", "LLM_MODEL",
              "PENNANT_DB", "PENNANT_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('# keys\nCH_API_KEY="abc"\nexport PENNANT_OFFLINE=1\nPENNANT_DB=x.db\n')
    s = load_settings(env)
    assert s.ch_api_key == "abc" and s.offline and s.db_path == "x.db"
    assert s.llm_model == "claude-sonnet-5-5"
    assert s.missing_keys() == ["LLAMA_CLOUD_API_KEY", "ANTHROPIC_API_KEY"]
    assert "abc" not in repr(s)
    monkeypatch.setenv("CH_API_KEY", "from-env")
    assert load_settings(env).ch_api_key == "from-env"


def test_settings_require():
    import pytest

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Settings().require("ANTHROPIC_API_KEY")
    assert Settings(ch_api_key="k").require("CH_API_KEY") == "k"


def test_notices_are_discovered_not_listed(tmp_path):
    """Any PDF in the folder is a notice; nothing is named in code."""
    for name in ("Acme_Holdings_2027_Notice_of_AGM.pdf", "Widget Co 2026 AGM notice.pdf",
                 "acme-holdings-2028-notice.pdf", "NoYear.pdf", "readme.txt"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    found = notices(tmp_path)
    assert found["widget-co"] == "Widget Co 2026 AGM notice.pdf"
    assert found["noyear"] == "NoYear.pdf"
    # two years of one company would share "acme-holdings": both keep their full stems
    assert {"acme-holdings-2027-notice-of-agm", "acme-holdings-2028-notice"} <= set(found)
    assert "readme" not in str(found)


def test_keys_and_labels_come_from_filenames():
    assert notice_label("Severn_Trent_2026_Notice_of_AGM.pdf") == "Severn Trent"
    assert meeting_key_for("Marks_and_Spencer_2026_Notice_of_AGM.pdf") == "marks-and-spencer"
    assert meeting_key_for("Société_Générale_2026_Notice.pdf") == "societe-generale"
    for key, filename in notices().items():  # the example notices in the repo
        assert (NOTICES_DIR / filename).exists() and meeting_key_for(filename) == key
