"""
Single entry point for all tests.

    python run_tests.py

Runs both suites and exits non-zero if anything fails, so it can be used
as a pre-deploy gate:

    python run_tests.py && git push
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(label: str, args: list) -> bool:
    print(f"\n{'=' * 64}\n  {label}\n{'=' * 64}")
    result = subprocess.run([sys.executable, "-X", "utf8"] + args, cwd=ROOT)
    return result.returncode == 0


def main():
    ok = True
    # Core: brain, scheduling, limits, stealth, signals, AI engine
    ok &= run("CORE UNIT TESTS", [os.path.join("tests", "test_suite.py")])
    # Integrations: Buffer/Facebook, article engine, fan-out, database
    ok &= run("INTEGRATION TESTS", [os.path.join("tests", "test_integrations.py")])
    # Regression/QA invariants
    ok &= run("QA INVARIANT CHECKS", [os.path.join("tests", "qa_check.py")])
    # Pinterest agent: sourcing, selection and the compliance gate
    ok &= run("PIN AGENT TESTS",
              ["-m", "unittest", "pin_agent.tests.test_pin_agent"])

    print(f"\n{'=' * 64}")
    print("  ALL TESTS PASSED" if ok else "  SOME TESTS FAILED")
    print(f"{'=' * 64}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
