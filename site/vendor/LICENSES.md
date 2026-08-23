# Vendored third-party code

`3d-force-graph.min.js` is the only third-party file the site ships. It is
the published UMD bundle of [3d-force-graph](https://github.com/vasturiano/3d-force-graph)
**v1.80.0**, downloaded verbatim from
`https://unpkg.com/3d-force-graph@1.80.0/dist/3d-force-graph.min.js` by
`make vendor` and pinned by sha256 in [`SHA256SUMS`](SHA256SUMS):

```
d96e738edcca580edd524730c1c6b05ed2efce028c23ca95db1bf43033a72e42  3d-force-graph.min.js
```

`make vendor` re-downloads and verifies that digest; the file is committed
so a build never depends on unpkg being up, and so a change to it shows up
in review as a diff rather than as a silent update.

## What the bundle contains

The dist bundle inlines 3d-force-graph's dependencies. These are the
packages it declares (versions are the ranges 3d-force-graph@1.80.0 pins;
the exact builds inside the bundle are whatever npm resolved when upstream
published it). Every one is MIT-licensed.

| Package | Declared range | License | Upstream |
|---|---|---|---|
| 3d-force-graph | 1.80.0 (this file) | MIT | https://github.com/vasturiano/3d-force-graph |
| three | `>=0.179 <1` | MIT | https://github.com/mrdoob/three.js |
| three-forcegraph | `1` | MIT | https://github.com/vasturiano/three-forcegraph |
| three-render-objects | `^1.41` | MIT | https://github.com/vasturiano/three-render-objects |
| kapsule | `^1.16` | MIT | https://github.com/vasturiano/kapsule |
| accessor-fn | `1` | MIT | https://github.com/vasturiano/accessor-fn |

`three-forcegraph` in turn brings in
[d3-force-3d](https://github.com/vasturiano/d3-force-3d), the
three-dimensional fork of Mike Bostock's `d3-force`, which is what actually
lays the graph out. Its licence was checked directly against the registry
(`npm view d3-force-3d license` -> `MIT`, v3.0.6), because the upstream
`d3-force` it derives from is ISC rather than MIT.

## MIT License

Copyright (c) Vasco Asturiano — 3d-force-graph, three-forcegraph,
three-render-objects, kapsule, accessor-fn, d3-force-3d.

Copyright (c) 2010 three.js authors — three.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Upgrading

1. Bump `FG_VERSION` in the `Makefile`.
2. Delete the old digest line, run `make vendor` (it will fail the check),
   record the new `shasum -a 256 site/vendor/3d-force-graph.min.js` output
   in `SHA256SUMS`, and run `make vendor` again to confirm it verifies.
3. Refresh the table above from the new release's `package.json`.
4. Commit the bundle, the digest, and this file together.
