# Data

`raw/PT.zip` is the EuroCrops Portugal 2021 parcel layer, included unchanged so
that every result in this repository can be reproduced without a download. The
engine reads the archive in place (`dss.spatial.vector_source`); there is no
need to unzip it.

| | |
|---|---|
| Dataset | EuroCrops, Portugal 2021 (`PT_2021_EC21` shapefile inside the archive) |
| Source | Zenodo record 10118572, <https://zenodo.org/records/10118572/files/PT.zip> |
| MD5 | `3dba9c89c559b34d57acd286505bcb66` (verify with `md5sum data/raw/PT.zip`) |
| Content | 100,000 parcel polygons with HCAT crop codes, ETRS89 / Portugal TM06 (EPSG:3763) |
| Licence | CC BY-SA 4.0, as stated by the EuroCrops project |

When using the data, cite EuroCrops and the Portuguese source it harmonises:

> Schneider, M., Schelte, T., Schmitz, F. & Körner, M. (2023). EuroCrops: the
> largest harmonized open crop dataset across the European Union. *Scientific
> Data* 10, 612. doi:10.1038/s41597-023-02517-0

The file is redistributed here under the same licence, without modification.
