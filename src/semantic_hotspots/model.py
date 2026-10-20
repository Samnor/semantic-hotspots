"""Portable model metadata. Connection strings and partition queries are not retained."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote


class ModelError(ValueError):
    """An input cannot be interpreted unambiguously."""


def object_id(kind: str, table: str, name: str = "") -> str:
    # Percent encoding avoids collisions when names contain slashes or punctuation.
    return f"{kind}:{quote(table, safe='')}/{quote(name, safe='')}"


@dataclass
class Entity:
    kind: str
    table: str
    name: str
    expression: str | None = None
    source: str = ""
    line: int = 1
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return object_id(self.kind, self.table, self.name)

    @property
    def qualified_name(self) -> str:
        if self.kind == "table":
            return "'" + self.table.replace("'", "''") + "'"
        return "'" + self.table.replace("'", "''") + "'[" + self.name.replace("]", "]]") + "]"


@dataclass
class Table:
    name: str
    storage_modes: set[str] = field(default_factory=set)
    source: str = ""

    @property
    def id(self) -> str:
        return object_id("table", self.name)


@dataclass
class Relationship:
    name: str
    from_table: str = ""
    from_column: str = ""
    to_table: str = ""
    to_column: str = ""
    active: bool = True
    cross_filter: str = "oneDirection"
    from_cardinality: str = "many"
    to_cardinality: str = "one"


@dataclass
class Model:
    name: str = "Model"
    entities: dict[str, Entity] = field(default_factory=dict)
    tables: dict[str, Table] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_format: str = ""

    def add_table(self, name: str, source: str = "") -> Table:
        existing = next((t for n, t in self.tables.items() if n.casefold() == name.casefold()), None)
        if existing:
            return existing
        table = Table(name, source=source)
        self.tables[name] = table
        entity = Entity("table", name, "", source=source)
        self.entities[entity.id] = entity
        return table

    def add(self, entity: Entity) -> Entity:
        if entity.kind != "function":
            table = self.add_table(entity.table, entity.source)
            entity.table = table.name
        duplicate = next((e for e in self.entities.values() if e.kind == entity.kind
                          and e.table.casefold() == entity.table.casefold()
                          and e.name.casefold() == entity.name.casefold()), None)
        if duplicate:
            raise ModelError(f"Duplicate {entity.kind}: {entity.qualified_name}")
        self.entities[entity.id] = entity
        return entity

    @property
    def measures(self) -> list[Entity]:
        return [e for e in self.entities.values() if e.kind == "measure"]

    def measure(self, name: str, table: str | None = None) -> Entity:
        if name in self.entities and self.entities[name].kind == "measure":
            return self.entities[name]
        found = [m for m in self.measures if m.name.casefold() == name.casefold()
                 and (table is None or m.table.casefold() == table.casefold())]
        if len(found) != 1:
            raise ModelError(f"Expected one measure named {name!r}; found {len(found)}. Supply its table or ID.")
        return found[0]
