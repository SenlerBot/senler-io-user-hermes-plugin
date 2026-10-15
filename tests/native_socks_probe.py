"""Run the native Relay probe through an isolated loopback SOCKS5 proxy."""
import argparse
import asyncio
from pathlib import Path
import sys


async def run(source):
    connections = []
    failures = []
    tasks = set()

    async def forward(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.write_eof()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        remote = None
        try:
            async with asyncio.timeout(5):
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                assert version == 5 and 0 in methods
                writer.write(b"\x05\x00")
                await writer.drain()
                version, command, reserved, kind = await reader.readexactly(4)
                assert (version, command, reserved) == (5, 1, 0)
                if kind == 1:
                    host = ".".join(str(byte) for byte in await reader.readexactly(4))
                else:
                    assert kind == 3, "Only IPv4 and domain requests are expected"
                    length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(length)).decode("ascii")
                port = int.from_bytes(await reader.readexactly(2), "big")
                assert host == "127.0.0.1", "The probe must never forward outside loopback"
                upstream, remote = await asyncio.open_connection(host, port)
                connections.append(port)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + port.to_bytes(2, "big"))
                await writer.drain()
            await asyncio.gather(forward(reader, remote), forward(upstream, writer))
        except Exception as error:
            failures.append(error)
        finally:
            for stream in (writer, remote):
                if stream is not None:
                    stream.close()
                    await stream.wait_closed()
            tasks.discard(task)

    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        # The synthetic child needs no profile, credentials or inherited proxy settings.
        env = {"HTTPS_PROXY": f"socks5h://127.0.0.1:{port}", "NO_PROXY": "",
               "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("native_relay_probe.py")),
            "--source-dir", str(source), env=env,
        )
        try:
            assert await asyncio.wait_for(process.wait(), 30) == 0, "Native probe failed via SOCKS"
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            server.close()
            await server.wait_closed()
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
    assert not failures, failures
    assert len(connections) == 2, "Both native connections must use the configured SOCKS proxy"
    print("PASS: native Relay handshake, event ACKs and report-only mode through HTTPS_PROXY=socks5h.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.source_dir))
