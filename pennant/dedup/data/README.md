# Nickname list

`nicknames.csv` is `names.csv` from [carltonnorthern/nicknames](https://github.com/carltonnorthern/nicknames),
commit `ed160b3af2d17e1af8ebd570f10a10e4dade4ff4` (2 Aug 2026), unchanged. It is licensed under
the Apache License 2.0; the licence is in `nicknames-LICENSE.txt`.

Each row is `name,has_nickname,nickname` (for example `thomas,has_nickname,tom`). `dedup/nicknames.py`
reads it to anchor a notice's familiar first name ("Tom Delay") to the formal name on Companies
House ("DELAY, Thomas"). To update it, replace the file with a newer commit's `names.csv` and
change the commit above.
