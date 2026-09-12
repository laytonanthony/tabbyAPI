import unittest
from pathlib import Path

import deepseek_routing_patch

DEPLOYED_ROUTER = Path(
    '/media/anthony/UltraGPT/Zeta WEBUI/Zeta WEBUI/backend/open_webui/routers/openai.py'
)


class DeepSeekRoutingPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DEPLOYED_ROUTER.exists():
            raise unittest.SkipTest('deployed Zeta router is not available')

    def test_installed_source_is_complete_and_idempotent(self):
        with DEPLOYED_ROUTER.open(encoding='utf-8') as handle:
            source = handle.read()
        self.assertEqual(deepseek_routing_patch.transform(source), source)

    def test_deepseek_messages_url_is_protocol_specific(self):
        namespace = {}
        exec(
            'from urllib.parse import urlparse\n' +
            source_function('_get_generic_proxy_request_url'),
            namespace,
        )
        build = namespace['_get_generic_proxy_request_url']
        self.assertEqual(
            build('https://api.deepseek.com/v1', 'messages'),
            'https://api.deepseek.com/anthropic/v1/messages',
        )
        self.assertEqual(
            build('http://127.0.0.1:8000/v1', 'messages'),
            'http://127.0.0.1:8000/v1/messages',
        )


def source_function(name):
    with DEPLOYED_ROUTER.open(encoding='utf-8') as handle:
        source = handle.read()
    start = source.index(f'def {name}(')
    end = source.index('\n\ndef ', start + 1)
    return source[start:end]


if __name__ == '__main__':
    unittest.main()
