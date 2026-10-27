"""prompts.md files: named sections, exact text, and the extract backend registry."""

from pathlib import Path

import pytest

from pennant.core.config import Settings
from pennant.extract import backends, llamaextract
from pennant.prompts import load_prompts, prompt

PACKAGE = Path(__file__).resolve().parents[1] / "pennant"


def test_sections_are_returned_byte_for_byte(tmp_path):
    md = tmp_path / "prompts.md"
    md.write_text("# Title\n\n## first_v1\n\nWhen it is sent.\n\n````text\nLine one.\n"
                  "```json\n{\"a\": 1}\n```\n````\n\n## second_v1\n\n````text\nTwo.\n````\n")
    assert load_prompts(md) == {"first_v1": 'Line one.\n```json\n{"a": 1}\n```\n',
                                "second_v1": "Two.\n"}
    with pytest.raises(KeyError, match="no prompt 'third_v1'"):
        prompt(md, "third_v1")


def test_every_package_prompt_file_parses():
    files = sorted(PACKAGE.glob("*/prompts.md"))
    assert files, "no prompts.md found"
    for f in files:
        assert load_prompts(f), f


def test_extract_system_prompt_is_the_one_the_recordings_were_keyed_on():
    """Moving the prompt into prompts.md must not change a byte: it is in the LlamaExtract
    cache key, so a change would invalidate every recorded extract."""
    fixtures = sorted(llamaextract.FIXTURES.glob("*.json"))
    assert fixtures
    for f in fixtures:
        recorded = llamaextract.json.loads(f.read_text())["config"]["system_prompt"]
        assert recorded == llamaextract.SYSTEM_PROMPT, f.name


def test_extract_backend_follows_the_setting_and_refuses_unknown_names():
    assert backends.backend(Settings()) is llamaextract
    for b in backends.all_backends():
        assert {"NAMESPACE", "notice_key", "extract_notice"} <= set(dir(b))
    with pytest.raises(ValueError, match="no such extract backend"):
        backends.backend(Settings(extract_model="some-other-model"))
