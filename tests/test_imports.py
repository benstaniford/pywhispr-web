#!/usr/bin/env python3
"""
Test script to verify all required imports work correctly

Runs two ways: as a script (pretty output, non-zero exit on failure, which is how
scripts/test-all and CI invoke it) and as a normal unittest module under pytest.
The checks live in functions rather than at import time so that collecting this
file cannot abort the whole test run.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_required():
    """Imports the app cannot run without. Returns a list of failure messages."""
    failures = []

    try:
        from flask import Flask  # noqa: F401 - import is the test
        print("✅ Flask import successful")
    except ImportError as e:
        print(f"❌ Flask import failed: {e}")
        failures.append(f"flask: {e}")

    try:
        import requests
        print(f"✅ requests import successful (version: {requests.__version__})")

        # Test that requests can handle basic functionality (without making actual requests)
        # This ensures the module is properly installed and functional
        requests.Session()
        print("✅ requests basic functionality works")
    except ImportError as e:
        print(f"❌ requests import failed: {e}")
        failures.append(f"requests: {e}")
    except Exception as e:
        print(f"❌ requests functionality test failed: {e}")
        failures.append(f"requests functionality: {e}")

    try:
        from werkzeug.serving import WSGIRequestHandler  # noqa: F401 - import is the test
        print("✅ Werkzeug import successful")
    except ImportError as e:
        print(f"❌ Werkzeug import failed: {e}")
        failures.append(f"werkzeug: {e}")

    # The app's own modules: a syntax error or a bad import here would otherwise
    # only surface when the container starts.
    try:
        import pywhispr_client  # noqa: F401 - import is the test
        print("✅ pywhispr_client import successful")
    except Exception as e:
        print(f"❌ pywhispr_client import failed: {e}")
        failures.append(f"pywhispr_client: {e}")

    try:
        import app  # noqa: F401 - import is the test
        print("✅ app import successful")
    except Exception as e:
        print(f"❌ app import failed: {e}")
        failures.append(f"app: {e}")

    return failures


def check_serving():
    """Imports only needed to serve in production. Absence is a warning.

    Gunicorn is not needed to run the tests or the Flask dev server, and a
    developer who skipped `pip install -r requirements.txt` should still be able
    to run the suite. The Docker container tests prove it is present in the image.
    """
    warnings = []
    try:
        import gunicorn  # noqa: F401 - import is the test
        print("✅ Gunicorn import successful")
    except ImportError as e:
        print(f"⚠️  Gunicorn import failed: {e}")
        warnings.append(f"gunicorn: {e}")
    return warnings


class TestImports(unittest.TestCase):
    def test_required_imports_are_available(self):
        self.assertEqual(check_required(), [])


def main():
    print("Testing all imports...")
    failures = check_required()
    warnings = check_serving()

    print("\n🔍 Summary:")
    print("All imports should work for both local development and Docker deployment")
    print("requests is required for Docker health checks and for reaching PyWhispr")
    print("gunicorn is used for production deployment")

    if warnings:
        print(f"\n⚠️  {len(warnings)} optional dependency missing (fine for local testing):")
        for warning in warnings:
            print(f"   - {warning}")

    if failures:
        print(f"\n❌ {len(failures)} required import check(s) failed:")
        for failure in failures:
            print(f"   - {failure}")
        return 1

    print("\n✅ All required import checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
