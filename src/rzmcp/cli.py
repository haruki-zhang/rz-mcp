"""Call the query layer from a terminal, without MCP (same functions the MCP tools use).

    PYTHONPATH=src python3 -m rzmcp.cli lookup_register WDTSET_2 address_space=CM33_NS
    PYTHONPATH=src python3 -m rzmcp.cli search_docs "boot mode pin" limit=5
    PYTHONPATH=src python3 -m rzmcp.cli get_section 7.4.2.5
"""
import inspect
import sys

from . import query as Q

TOOLS = ["list_documents", "search_docs", "get_toc", "get_section", "read_pages", "lookup_register", "search_registers"]


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in TOOLS:
        sys.exit(f"usage: python -m rzmcp.cli <{'|'.join(TOOLS)}> [positional...] [key=value...]")
    fn = getattr(Q, argv[0])
    pos, kw = [], {}
    for a in argv[1:]:
        k, sep, v = a.partition("=")
        if sep and k.isidentifier() and k in inspect.signature(fn).parameters:
            kw[k] = v
        else:
            pos.append(a)
    print(fn(*pos, **kw))


if __name__ == "__main__":
    main(sys.argv[1:])
