"""Development-only validation against versioned Microsoft PBIP/PBIR schemas.

Requires jsonschema; performs network reads only from Microsoft's schema host.
This checks document structure, not Desktop loading, BIM types, refresh or DAX.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import urlopen


def main():
    from jsonschema import Draft7Validator
    from referencing import Registry, Resource
    root=Path(sys.argv[1]);cache={}
    def retrieve(uri):
        if not uri.startswith('https://developer.microsoft.com/json-schemas/'):
            raise ValueError('Schema reference is outside the Microsoft schema host')
        if uri not in cache:
            with urlopen(uri,timeout=30) as response:
                content=response.read(4*1024*1024+1)
            if len(content)>4*1024*1024:raise ValueError('Schema exceeds the validation size limit')
            cache[uri]=json.loads(content)
        return Resource.from_contents(cache[uri])
    registry=Registry(retrieve=retrieve);count=0
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix not in {'.json','.pbip','.pbir','.pbism'}:continue
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict) or '$schema' not in value:continue
        schema=retrieve(value['$schema']).contents
        Draft7Validator(schema,registry=registry).validate(value);count+=1
    if count!=8:raise ValueError('Expected the eight documents of a one-page, one-visual project')
    print(f'Validated {count} PBIP/PBIR/PBISM documents against versioned Microsoft schemas. Desktop/engine validation is separate.')


if __name__=='__main__':main()
