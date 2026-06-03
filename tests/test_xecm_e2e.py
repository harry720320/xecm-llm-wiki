"""End-to-end test: xECM workspace -> discover -> download -> verify.

Requires xECM demo instance at 192.168.0.29.
Run: python tests/test_xecm_e2e.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "api"))


async def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)

        # Create .xecm_config (not used directly in E2E but tested for parsing)
        (ws / ".xecm_config").write_text(
            "XECM_URL=http://192.168.0.29/otcs/cs.exe\n"
            "XECM_USERNAME=admin\n"
            "XECM_PASSWORD=OpenText1\n"
            "XECM_WORKSPACE=LLM Wiki\n"
        )

        # Verify config loader
        from config import load_xecm_config
        cfg = load_xecm_config(str(ws))
        assert cfg["XECM_URL"] == "http://192.168.0.29/otcs/cs.exe"
        print("Config loader: OK")

        # Connect and discover
        from infra.xecm import XecmClient, XecmSourceReader

        client = XecmClient(
            url="http://192.168.0.29/otcs/cs.exe",
            username="admin",
            password="OpenText1",
        )

        try:
            reader = XecmSourceReader(client)
            docs = await reader.discover(2000)
            print(f"Discovered {len(docs)} documents in Enterprise workspace")

            for doc in docs[:5]:
                print(f"  - [{doc.file_type}] {doc.name} ({doc.size} bytes, node {doc.node_id})")

            assert len(docs) >= 1, "Expected at least 1 document"

            # Download first doc
            first_doc = docs[0]
            cache_dir = ws / ".llmwiki" / "cache" / "sources"
            path = await reader.download_to_cache(first_doc.node_id, cache_dir)
            assert path.is_file(), f"Downloaded file should exist: {path}"
            print(f"Cached: {path} ({path.stat().st_size} bytes)")

            content = path.read_bytes()
            assert len(content) > 0, "Downloaded file should not be empty"
            print("Content download: OK")

            # Verify Content Server version is accessible
            node = await client.get_node(2000)
            assert node["id"] == 2000
            print(f"Content Server type: {node.get('type_name', 'unknown')}")

            print("\n=== All E2E tests passed ===")

        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
