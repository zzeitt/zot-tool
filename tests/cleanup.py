"""Clean up leftover items/collections from test runs in the test library.

Uses the same env vars as conftest.py:
  ZOTERO_API_KEY           — required
  ZOTERO_TEST_LIBRARY_ID   — required
  ZOTERO_TEST_LIBRARY_TYPE — default: group
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from pyzotero import zotero
import requests

LIBRARY_ID = os.environ['ZOTERO_TEST_LIBRARY_ID']
LIBRARY_TYPE = os.environ.get('ZOTERO_TEST_LIBRARY_TYPE', 'group')
API_BASE = f"https://api.zotero.org/{LIBRARY_TYPE}s/{LIBRARY_ID}"

api_key = os.environ.get('ZOTERO_API_KEY', '')
if not api_key:
    print("Error: ZOTERO_API_KEY not set")
    sys.exit(1)

zot = zotero.Zotero(LIBRARY_ID, LIBRARY_TYPE, api_key)

# Delete leftover items
items = list(zot.everything(zot.items()))
for item in items:
    try:
        zot.delete_item(item)
        print(f'Deleted item: {item["key"]}')
    except Exception as e:
        print(f'Failed to delete item {item["key"]}: {e}')

# Delete leftover collections (Misc--* that aren't Test-Misc)
cols = list(zot.everything(zot.collections()))
for c in cols:
    name = c['data'].get('name', '')
    if name.startswith('Misc--'):
        ck = c['key']
        r = requests.get(
            f'{API_BASE}/collections/{ck}',
            headers={'Authorization': f'Bearer {api_key}', 'Zotero-API-Version': '3'},
        )
        if r.status_code == 200:
            data = r.json()
            ver = data.get('version') or data.get('data', {}).get('version')
            if ver:
                requests.delete(
                    f'{API_BASE}/collections/{ck}',
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Zotero-API-Version': '3',
                        'If-Unmodified-Since-Version': str(ver),
                    },
                )
                print(f'Deleted collection: {name} ({ck})')

print('Cleanup done.')
