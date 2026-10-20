"""Windows CI: load official client APIs and serialize .NET values, without DAX.

Downloads a pinned Microsoft NuGet package into a temporary directory. No client
DLL or engine is distributed, and no endpoint is contacted by this check.
"""
from __future__ import annotations

import sys
import subprocess
import tempfile
from pathlib import Path


def main():
    if sys.platform != 'win32':
        raise SystemExit('This client integration check requires Windows')
    from semantic_hotspots.client_setup import install_adomd
    with tempfile.TemporaryDirectory() as directory:
        root = install_adomd(Path(directory)/'client').parent
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--check', str(root)], check=True, timeout=60)
    print('Official ADOMD client loaded; invariant .NET value serialization passed. No DAX engine execution was tested.')


def check(root):
    from semantic_hotspots._adomd_worker import cell, connection_string, load_adomd
    factory = load_adomd(root, 'netfx')
    connection = factory(connection_string('localhost:12345', 'Synthetic'))
    command = connection.CreateCommand()
    command.CommandText = 'EVALUATE ROW("Value",1)'
    command.CommandTimeout = 2
    assert command.CommandTimeout == 2
    command.Dispose()
    connection.Dispose()
    import clr
    clr.AddReference('System.Data')
    from System import Array, Boolean, DateTime, DBNull, Decimal, Double, Int64, Object, String
    from System.Data import DataTable
    from System.Globalization import CultureInfo
    from System.Threading import Thread
    Thread.CurrentThread.CurrentCulture = CultureInfo('fr-FR')
    table = DataTable()
    kinds = [Double, Decimal, Boolean, Boolean, DateTime, Int64, String]
    for i, kind in enumerate(kinds):
        table.Columns.Add('Value'+str(i), kind)
    table.Rows.Add(Array[Object]([Double(1.25), Decimal.Parse('123456789012345678.1234', CultureInfo.InvariantCulture),
                                 Boolean(False), Boolean(True), DateTime(2024,1,2,3,4,5),
                                 Int64(9223372036854775807), DBNull.Value]))
    reader = table.CreateDataReader()
    try:
        assert reader.Read()
        values = [cell(reader,i) for i in range(reader.FieldCount)]
        expected = ['1.25', '123456789012345678.1234', '0', '1', '2024-01-02T03:04:05.0000000', '9223372036854775807', '']
        assert values == expected, (values, expected)
    finally:
        reader.Dispose()
        table.Dispose()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--check':
        check(Path(sys.argv[2]))
    else:
        main()
