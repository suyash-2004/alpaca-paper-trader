"""Load the day-trader skill pack's own scripts (unchanged, from .claude/skills) as Python modules."""
import importlib.util
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / ".claude" / "skills"


def load_skill(folder, script):
    spec = importlib.util.spec_from_file_location("skill_" + script, SKILLS / folder / (script + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
