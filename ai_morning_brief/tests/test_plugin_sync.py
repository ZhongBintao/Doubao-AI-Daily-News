import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "ai-daily-news-studio"


class PluginSyncTests(unittest.TestCase):
    def test_plugin_is_v041_and_project_skill_mirrors_match(self):
        plugin = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue(plugin["version"].startswith("0.5.1+"))
        pairs = [
            ("skills/ai-brief-editorial-writer/SKILL.md", ".agents/skills/ai-brief-editorial-writer/SKILL.md"),
            ("skills/ai-brief-editorial-writer/references/editorial-contract.md", ".agents/skills/ai-brief-editorial-writer/references/editorial-contract.md"),
            ("skills/ai-brief-cover-generator/SKILL.md", ".agents/skills/ai-brief-cover-generator/SKILL.md"),
            ("skills/ai-brief-release-kit/SKILL.md", ".agents/skills/ai-brief-release-kit/SKILL.md"),
            ("skills/ai-brief-speech-quality/SKILL.md", ".agents/skills/ai-brief-speech-quality/SKILL.md"),
            ("skills/ai-daily-news-studio-workflow/references/editorial-planning.md", ".agents/skills/ai-signal-morning-brief/references/editorial-planning.md"),
            ("skills/ai-daily-news-studio-workflow/references/x-screenshot-capture.md", ".agents/skills/ai-signal-morning-brief/references/x-screenshot-capture.md"),
        ]
        for plugin_relative, mirror_relative in pairs:
            with self.subTest(plugin_relative=plugin_relative):
                self.assertEqual(
                    (PLUGIN / plugin_relative).read_bytes(),
                    (ROOT / mirror_relative).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
