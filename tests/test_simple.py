import unittest
import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

class TestSimple(unittest.TestCase):
    
    def setUp(self):
        """Set up test client"""
        self.app = app.test_client()
        self.app.testing = True
    
    def test_health_endpoint(self):
        """Test that the health endpoint works correctly"""
        response = self.app.get('/health')
        self.assertEqual(response.status_code, 200)
        
        # Check that response is JSON
        data = response.get_json()
        self.assertIsNotNone(data)
        
        # Check required fields are present
        self.assertIn('status', data)
        self.assertEqual(data['status'], 'healthy')
    
    def test_main_page_loads_without_credentials(self):
        """The app has no authentication, so the editor is served directly"""
        response = self.app.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'PyWhispr Web', response.data)
        # The editor and its record button are the whole point of the page.
        self.assertIn(b'id="editor"', response.data)
        self.assertIn(b'id="record"', response.data)

    def test_settings_page_loads_without_credentials(self):
        """Test that the settings page is served directly"""
        response = self.app.get('/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'PyWhispr servers', response.data)

    def test_api_endpoint_loads_without_credentials(self):
        """Test that the server configuration API needs no session"""
        response = self.app.get('/api/servers')
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertIsNotNone(data)
        self.assertIn('servers', data)
        self.assertIn('cache_ttl_seconds', data)

    def test_there_is_no_login_route(self):
        """A leftover /login would mean auth was only half removed"""
        self.assertEqual(self.app.get('/login').status_code, 404)
        self.assertEqual(self.app.get('/logout').status_code, 404)

    def test_requests_module_available(self):
        """Test that requests module is available for health checks"""
        try:
            import requests
            # Test that we can create a session (basic functionality)
            session = requests.Session()
            self.assertIsNotNone(session)
        except ImportError:
            self.fail("requests module is not available - required for Docker health checks")

if __name__ == '__main__':
    unittest.main()