from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "SKILL.md"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    marker = "\n---\n"
    end = text.find(marker, 4)
    assert end != -1, "SKILL.md frontmatter must be closed with ---"
    data: dict[str, str] = {}
    current: str | None = None
    for raw in text[4:end].splitlines():
        if not raw.strip():
            continue
        if raw[0].isspace():
            if current:
                data[current] = (data[current] + " " + raw.strip()).strip()
            continue
        key, separator, value = raw.partition(":")
        assert separator, f"invalid frontmatter line: {raw!r}"
        key = key.strip()
        value = value.strip()
        if value in {">-", ">", "|", "|-"}:
            value = ""
        data[key] = value
        current = key
    return data, text[end + len(marker):]


class SkillStructureTests(unittest.TestCase):
    def test_skill_frontmatter(self) -> None:
        data, body = parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
        self.assertEqual(data.get("name"), "session-reliability")
        description = data.get("description", "")
        self.assertTrue(description, "frontmatter description must be non-empty")
        lowered = description.lower()
        for term in ("persist", "recover", "multi-step", "continue", "resume", "prior work"):
            self.assertIn(term, lowered, f"description should mention {term!r}")
        self.assertIn("compatibility", data)
        self.assertTrue(data["compatibility"].lower().startswith("requires"))
        self.assertNotIn("every session start", body.lower())
        self.assertNotIn("at every session start", body.lower())

    def test_resource_layout(self) -> None:
        for directory in ("assets", "scripts", "references"):
            self.assertTrue((ROOT / directory).is_dir(), f"missing directory: {directory}")
        self.assertFalse((ROOT / "templates").exists(), "templates directory must be migrated to assets")
        for asset in ("TASK.md", "CHECKPOINT.md", "state.json", "session.json"):
            self.assertTrue((ROOT / "assets" / asset).is_file(), f"missing asset: assets/{asset}")

    def test_skill_md_references_exist(self) -> None:
        text = SKILL_MD.read_text(encoding="utf-8")
        references = set(re.findall(r"`(references/[A-Za-z0-9_.-]+\.md)`", text))
        scripts = set(re.findall(r"`(scripts/[A-Za-z0-9_.-]+\.py)`", text))
        self.assertIn("references/cli.md", references)
        self.assertIn("scripts/init.py", scripts)
        for relative in references | scripts:
            self.assertTrue((ROOT / relative).is_file(), f"SKILL.md references missing file: {relative}")

    def test_markdown_relative_links_exist(self) -> None:
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for markdown in sorted(ROOT.rglob("*.md")):
            if ".git" in markdown.parts:
                continue
            for target in link_pattern.findall(markdown.read_text(encoding="utf-8")):
                if target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                path_text = target.split("#", 1)[0]
                self.assertTrue(path_text, f"empty link in {markdown}")
                resolved = (markdown.parent / path_text).resolve()
                self.assertTrue(resolved.exists(), f"broken link in {markdown}: {target}")

    def test_scripts_work_outside_skill_cwd_and_use_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "external-workspace"
            workspace.mkdir()
            skill_store = ROOT / ".agents" / "store" / "session-reliability"
            skill_store_existed = skill_store.exists()
            env = os.environ.copy()
            env.pop("SESSION_RELIABILITY_STORE", None)
            env.pop("SESSION_RELIABILITY_WORKSPACE", None)

            init = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "init.py"),
                    "--workspace", str(workspace),
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            store = workspace / ".agents" / "store" / "session-reliability"
            self.assertTrue((store / "index.json").is_file())
            self.assertTrue((store / "sessions").is_dir())

            create = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "checkpoint.py"),
                    "--workspace", str(workspace),
                    "create-task",
                    "--task-id", "task-test-cwd",
                    "--title", "CWD Independence",
                    "--objective", "Prove assets resolve from skill root.",
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            task_dir = store / "tasks" / "task-test-cwd"
            task_md = (task_dir / "TASK.md").read_text(encoding="utf-8")
            self.assertIn("CWD Independence", task_md)
            state = json.loads((task_dir / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(state["task_id"], "task-test-cwd")

            if not skill_store_existed:
                self.assertFalse(skill_store.exists(), "runtime state was written into the skill installation")


if __name__ == "__main__":
    unittest.main()
