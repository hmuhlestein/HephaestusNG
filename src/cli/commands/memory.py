"""heph memory — Knowledge base operations."""

from src.cli.utils import api_post, output, require_backend, truncate


def register(subparsers):
    p = subparsers.add_parser("memory", help="Knowledge base operations")
    sub = p.add_subparsers(dest="subcommand")

    s = sub.add_parser("search", help="Search the knowledge base")
    s.add_argument("query", help="Search query")
    s.add_argument("--limit", type=int, default=10, help="Max results")
    s.add_argument("--type", dest="memory_type", help="Filter by memory type")
    s.set_defaults(func=search)

    sv = sub.add_parser("save", help="Save to knowledge base")
    sv.add_argument("content", help="Content to save")
    sv.add_argument(
        "--type", dest="memory_type", default="discovery", help="Memory type"
    )
    sv.add_argument("--tags", nargs="*", default=[], help="Tags")
    sv.set_defaults(func=save)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def search(args):
    if not require_backend(args):
        return 1
    payload = {"query": args.query, "limit": args.limit}
    if args.memory_type:
        payload["memory_type"] = args.memory_type
    data = api_post(args, "/search_memory", payload)
    results = data.get("results", []) if isinstance(data, dict) else data or []

    output(args, results, _print_search_results)
    return 0


def _print_search_results(results):
    if not results:
        print("No results.")
        return
    for i, r in enumerate(results, 1):
        score = r.get("score", 0)
        content = r.get("content", "")
        mtype = r.get("memory_type", "")
        print(f"  [{i}] ({score:.2f}) [{mtype}] {truncate(content, 120)}")
        meta = r.get("metadata", {})
        if meta:
            print(f"      Tags: {meta.get('tags', [])}")


def save(args):
    if not require_backend(args):
        return 1
    payload = {
        "content": args.content,
        "memory_type": args.memory_type,
        "tags": args.tags,
    }
    data = api_post(args, "/save_memory", payload)
    output(args, data, lambda d: print(f"Saved: {d.get('memory_id', d)}"))
    return 0
