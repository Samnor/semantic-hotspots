"""Consumer → dependency graphs with dependency-first ordering and cycle reporting."""
from __future__ import annotations

import heapq
from collections import defaultdict

from .dax import ExpressionInfo, Resolver
from .model import Model, ModelError
from .progress import emit


class CycleError(ModelError):
    pass


class DependencyGraph:
    def __init__(self, model: Model, *, progress=None):
        self.model = model
        self.ids = sorted(model.entities)
        self.index = {n: i for i, n in enumerate(self.ids)}
        self.dependencies: dict[str, set[str]] = {n: set() for n in self.ids}
        self.consumers: dict[str, set[str]] = defaultdict(set)
        self.syntax: dict[str, ExpressionInfo] = {}
        self.diagnostics: list[str] = []
        resolver = Resolver(model)
        emit(progress, 'dependencies', 'Trace dependencies', completed=0, total=len(model.entities), unit='objects')
        for i, entity in enumerate(model.entities.values(), 1):
            emit(progress, 'dependencies', 'Trace dependencies', completed=i-1, total=len(model.entities), unit='objects', detail=entity.qualified_name)
            if entity.expression:
                try:
                    info = resolver.scan(entity.expression, entity)
                    self.syntax[entity.id] = info
                    self.dependencies[entity.id].update(r.target for r in info.references)
                except ModelError as exc:
                    self.diagnostics.append(f"{entity.qualified_name}: {exc}")
                    self.syntax[entity.id] = ExpressionInfo(diagnostics=[str(exc)], reference_blockers=[str(exc)])
            if entity.kind == "column":
                self.dependencies[entity.id].add(model.tables[entity.table].id)
            if entity.kind == "format_string" and entity.metadata.get("for_measure"):
                self.dependencies[entity.metadata['for_measure']].add(entity.id)
        emit(progress, 'dependencies', 'Trace dependencies', detail='Resolve dependency order and detect cycles')
        for consumer, deps in self.dependencies.items():
            for dep in deps:
                self.consumers[dep].add(consumer)
        remaining = {n: len(ds) for n, ds in self.dependencies.items()}
        ready = [n for n, count in remaining.items() if not count]
        heapq.heapify(ready)
        self.order: list[str] = []
        while ready:
            node = heapq.heappop(ready)
            self.order.append(node)
            for consumer in sorted(self.consumers[node]):
                remaining[consumer] -= 1
                if remaining[consumer] == 0:
                    heapq.heappush(ready, consumer)
        # These are blocked nodes, not necessarily all members of a cycle.
        self.blocked = sorted(set(self.ids) - set(self.order))
        self.cycles = self._cycles()
        self._masks: dict[str, int] = {}
        self.depths: dict[str, int] = {}
        for n in self.order:
            bits = 1 << self.index[n]
            for dep in self.dependencies[n]:
                bits |= self._masks[dep]
            self._masks[n] = bits
            self.depths[n] = max((self.depths[d] + 1 for d in self.dependencies[n]), default=0)

    def _cycles(self) -> list[list[str]]:
        """Iterative DFS: back-edge witnesses, not falsely labeled SCC membership."""
        color: dict[str, int] = {}
        cycles: list[list[str]] = []
        for root in self.blocked:
            if color.get(root):
                continue
            path: list[str] = [root]
            positions = {root: 0}
            color[root] = 1
            stack = [(root, iter(sorted(self.dependencies[root])))]
            while stack:
                node, children = stack[-1]
                dep = next(children, None)
                if dep is None:
                    color[node] = 2
                    stack.pop()
                    positions.pop(node, None)
                    path.pop()
                elif not color.get(dep):
                    color[dep] = 1
                    positions[dep] = len(path)
                    path.append(dep)
                    stack.append((dep, iter(sorted(self.dependencies[dep]))))
                elif color[dep] == 1:
                    cycles.append(path[positions[dep]:] + [dep])
        return cycles

    def mask(self, root: str) -> int:
        if root not in self.dependencies:
            raise ModelError(f"Unknown graph node: {root}")
        if root not in self._masks:
            pending, seen = [root], set()
            while pending:
                n = pending.pop()
                if n not in seen:
                    seen.add(n)
                    pending.extend(self.dependencies[n] - seen)
            self._masks[root] = sum(1 << self.index[n] for n in seen)
        return self._masks[root]

    def dependency_ids(self, root: str, include_root: bool = False) -> list[str]:
        bits = self.mask(root)
        if not include_root:
            bits &= ~(1 << self.index[root])
        result = []
        while bits:
            low = bits & -bits
            result.append(self.ids[low.bit_length() - 1])
            bits ^= low
        return result

    def dependency_order(self, root: str | None = None) -> list[str]:
        wanted = set(self.ids if root is None else self.dependency_ids(root, True))
        if wanted.intersection(self.blocked):
            raise CycleError("Requested graph contains or depends on a cycle; see graph.cycles")
        return [n for n in self.order if n in wanted]

    def to_dict(self, root: str | None = None) -> dict:
        wanted = set(self.ids if root is None else self.dependency_ids(root, True))
        nodes = []
        for n in sorted(wanted):
            e = self.model.entities[n]
            nodes.append({"id": n, "type": e.kind, "table": e.table, "name": e.name,
                          "expression": e.expression, "source": e.source, "line": e.line})
            if n in self.syntax:
                info = self.syntax[n]
                nodes[-1]['expression_structure'] = {
                    'functions': info.functions, 'variables': info.variables,
                    'virtual_columns': info.virtual_columns,
                    'calls': [{'function': c.name, 'expression_line': (e.expression or '').count('\n', 0, c.start) + 1,
                               'arguments': [c.argument(e.expression or '', i) for i in range(len(c.arguments))],
                               'enclosing_calls': list(c.ancestors)} for c in info.calls],
                    'diagnostics': info.diagnostics,
                }
        edges = []
        for n in sorted(wanted):
            info = self.syntax.get(n)
            for dep in sorted(self.dependencies[n]):
                refs = [r for r in info.references if r.target == dep] if info else []
                edges.append({'from': n, 'to': dep,
                              'kind': 'expression_reference' if refs else 'metadata_dependency',
                              'textual_occurrences': len(refs),
                              'contexts': [{'offset': r.start, 'enclosing_calls': list(r.contexts),
                                            'call_arguments': [{'function': c.name, 'argument': i + 1}
                                                               for c in info.calls for i, (a, b) in enumerate(c.arguments)
                                                               if a <= r.start < b]} for r in refs]})
        acyclic = not wanted.intersection(self.blocked)
        return {"schema_version": "1.0", "root": root,
                "edge_meaning": "consumer -> something it depends on (static possibilities, not an execution plan)",
                "is_acyclic": acyclic,
                "evaluation_order": self.dependency_order(root) if acyclic else None,
                "cycle_witnesses": [c for c in self.cycles if set(c).intersection(wanted)],
                "nodes": nodes,
                "edges": edges,
                "unresolved": {n: self.syntax[n].unresolved for n in sorted(wanted)
                               if n in self.syntax and self.syntax[n].unresolved}}
