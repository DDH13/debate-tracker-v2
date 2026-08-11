"""Generate a Mermaid ER diagram from the SQLModel metadata.

Usage: python scripts/gen_er_diagram.py [output_file]
Defaults to printing to stdout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.models  # noqa: E402, F401  (registers all tables on SQLModel.metadata)
from sqlmodel import SQLModel  # noqa: E402

TYPE_MAP = {
    "INTEGER": "int",
    "VARCHAR": "string",
    "BOOLEAN": "bool",
    "FLOAT": "float",
    "DATE": "date",
    "DATETIME": "datetime",
}


def sql_type_name(col_type) -> str:
    raw = str(col_type).split("(")[0].upper()
    return TYPE_MAP.get(raw, raw.lower())


def build_mermaid() -> str:
    metadata = SQLModel.metadata
    lines = ["erDiagram"]

    # Relationships (foreign keys)
    seen_rels = set()
    for table in metadata.tables.values():
        for col in table.columns:
            for fk in col.foreign_keys:
                other = fk.column.table.name
                rel_key = (table.name, other, col.name)
                if rel_key in seen_rels:
                    continue
                seen_rels.add(rel_key)
                label = col.name.removesuffix("_id")
                # left symbol describes cardinality of `other` as seen from `table`;
                # a nullable FK means the child may reference zero parents.
                parent_symbol = "o|" if col.nullable else "||"
                lines.append(f'    {other} {parent_symbol}--o{{ {table.name} : "{label}"')

    # Entities (columns)
    for name, table in metadata.tables.items():
        lines.append(f"    {name} {{")
        for col in table.columns:
            attrs = []
            if col.primary_key:
                attrs.append("PK")
            if col.foreign_keys:
                attrs.append("FK")
            attr_str = " ".join(attrs)
            lines.append(f"        {sql_type_name(col.type)} {col.name} {attr_str}".rstrip())
        lines.append("    }")

    return "\n".join(lines)


if __name__ == "__main__":
    diagram = build_mermaid()
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            f.write(diagram + "\n")
    else:
        print(diagram)
