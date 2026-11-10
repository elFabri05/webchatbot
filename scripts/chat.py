"""Terminal client for the running app (until the web chat page exists).

Usage: uv run python scripts/chat.py [--url http://localhost:8000] [--conversation ID]
Prints each reply plus the tools used and token usage (incl. cache reads).
"""
import argparse
import json
import urllib.request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--conversation", default=None)
    args = ap.parse_args()
    conv = args.conversation
    print("Escribe tu mensaje (Ctrl+D para salir)")
    while True:
        try:
            msg = input("\ntú> ").strip()
        except EOFError:
            break
        if not msg:
            continue
        body = json.dumps({"message": msg, "conversation_id": conv}).encode()
        req = urllib.request.Request(f"{args.url}/chat", body,
                                     {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
        conv = out["conversation_id"]
        print(f"bot> {out['reply']}")
        tools = ", ".join(c["name"] + ("!" if c["is_error"] else "") for c in out["tool_calls"])
        u = out["usage"]
        print(f"     [{conv} | {out['status']} | tools: {tools or '-'} | in {u.get('input_tokens', 0)}"
              f" cache_read {u.get('cache_read_input_tokens', 0)}"
              f" cache_write {u.get('cache_creation_input_tokens', 0)} out {u.get('output_tokens', 0)}]")


if __name__ == "__main__":
    main()
