#!/usr/bin/env python3
"""Generate a lite Archon workflow from its parent + a manifest + overlays.

Usage: derive-lite.py <lane> [--out <path>] [--check]

  <lane>    api | bugfix  (reads setup/lite/<lane>.json)
  --out     write the YAML here instead of workflows/<manifest.name>.yaml
  --check   do not write; exit 1 if the shipped YAML differs from the
            regeneration (package.sh's LITE_DRIFT step)

The lite lane is DEFINED by three things and nothing else:
  1. setup/lite/<lane>.json   ordered node list, depends_on rewrites, loop
                              caps, port map, name/description, and the
                              declared artifact contracts (produces/consumes)
                              for EVERY selected node
  2. setup/lite/<lane>/       overlay files, whole-field replacement:
       <id>.prompt.md            replaces node.prompt
       <id>.bash.sh              replaces node.bash
       <id>.approval.md          replaces node.approval.message
       <id>.on_reject.md         replaces node.approval.on_reject.prompt
       <id>.node.yaml            a complete node (NEW nodes only)
       <loop>.<id>.bash.sh|.prompt.md   loop-body node fields
  3. workflows/<parent>.yaml  every retained node is the parent's bytes,
                              except: depends_on overrides, loop caps, and
                              the port map (a plain string substitution)

Hand-editing a generated YAML is forbidden; package.sh regenerates and diffs.

Contract check: every artifact a selected node `consumes` must be `produced`
by a selected node (or listed under `inherited_dynamic`, for artifacts the
parent builds from round directories / shell variables). A consumer without a
producer is a hard failure. A grep over the node bodies for
`$ARTIFACTS_DIR/<name>` is run as a WARNING only — it cannot see indirection.
"""
import argparse
import copy
import io
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHON = os.path.dirname(HERE)


class LiteError(SystemExit):
    def __init__(self, msg):
        super().__init__(f"DERIVE_LITE=FAIL {msg}")


# ---------------------------------------------------------------- YAML I/O
class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def dump_yaml(doc):
    buf = io.StringIO()
    yaml.dump(doc, buf, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=10**9, default_flow_style=False)
    return buf.getvalue()


def load_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------- overlays
OVERLAY_RE = re.compile(r"^(?P<id>[A-Za-z0-9-]+)(?:\.(?P<sub>[A-Za-z0-9-]+))?\.(?P<kind>prompt\.md|bash\.sh|approval\.md|on_reject\.md|node\.yaml)$")


def read_overlays(lane_dir):
    """Return {id: {kind: text}} and {loop: {sub: {kind: text}}}."""
    top, loops = {}, {}
    if not os.path.isdir(lane_dir):
        return top, loops
    for name in sorted(os.listdir(lane_dir)):
        path = os.path.join(lane_dir, name)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        m = OVERLAY_RE.match(name)
        if not m:
            raise LiteError(f"overlay file name not understood: {name}")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if m["sub"]:
            loops.setdefault(m["id"], {}).setdefault(m["sub"], {})[m["kind"]] = text
        else:
            top.setdefault(m["id"], {})[m["kind"]] = text
    return top, loops


def apply_field_overlays(node, ov, where):
    for kind, text in ov.items():
        if kind == "prompt.md":
            if "prompt" not in node:
                raise LiteError(f"{where}: prompt overlay on a node without a prompt")
            node["prompt"] = text
        elif kind == "bash.sh":
            if "bash" not in node:
                raise LiteError(f"{where}: bash overlay on a node without a bash body")
            node["bash"] = text
        elif kind == "on_reject.md":
            try:
                node["approval"]["on_reject"]["prompt"] = text
            except (KeyError, TypeError):
                raise LiteError(f"{where}: on_reject overlay on a node without approval.on_reject")
        elif kind == "approval.md":
            try:
                node["approval"]["message"] = text.strip()
            except (KeyError, TypeError):
                raise LiteError(f"{where}: approval overlay on a node without approval.message")
        elif kind == "node.yaml":
            raise LiteError(f"{where}: node.yaml overlay is only valid for NEW nodes (id not in the parent)")


def substitute_ports(obj, ports):
    if not ports:
        return obj
    if isinstance(obj, str):
        for a, b in ports.items():
            obj = obj.replace(a, b)
        return obj
    if isinstance(obj, list):
        return [substitute_ports(x, ports) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute_ports(v, ports) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- derive
def derive(lane):
    mpath = os.path.join(HERE, "lite", f"{lane}.json")
    if not os.path.isfile(mpath):
        raise LiteError(f"manifest missing: {mpath}")
    with open(mpath, encoding="utf-8") as fh:
        m = json.load(fh)
    for k in ("parent", "name", "description", "nodes", "contracts"):
        if k not in m:
            raise LiteError(f"manifest {lane}.json lacks '{k}'")
    parent_path = os.path.join(ARCHON, "workflows", m["parent"])
    parent = load_yaml(parent_path)
    pnodes = {n["id"]: n for n in parent["nodes"]}
    order = m["nodes"]
    if len(set(order)) != len(order):
        raise LiteError("manifest node list has duplicates")
    top_ov, loop_ov = read_overlays(os.path.join(HERE, "lite", lane))
    for oid in list(top_ov) + list(loop_ov):
        if oid not in order:
            raise LiteError(f"overlay for id not in the manifest node list: {oid}")

    dep_over = m.get("depends_on", {})
    loops = m.get("loops", {})
    ports = m.get("ports", {})
    for oid in list(dep_over) + list(loops):
        if oid not in order:
            raise LiteError(f"manifest references id not in the node list: {oid}")

    out_nodes = []
    for nid in order:
        if nid in pnodes:
            node = copy.deepcopy(pnodes[nid])
            ov = top_ov.get(nid, {})
            if "node.yaml" in ov:
                raise LiteError(f"{nid}: node.yaml overlay for a node that exists in the parent")
            apply_field_overlays(node, ov, nid)
            if nid in loops:
                if "loop_group" not in node:
                    raise LiteError(f"{nid}: loop cap on a non-loop node")
                node["loop_group"]["max_iterations"] = int(loops[nid]["max_iterations"])
            if nid in loop_ov:
                if "loop_group" not in node:
                    raise LiteError(f"{nid}: loop-body overlay on a non-loop node")
                body = {b["id"]: b for b in node["loop_group"]["nodes"]}
                for sub, sov in loop_ov[nid].items():
                    if sub not in body:
                        raise LiteError(f"{nid}.{sub}: loop-body id not in the parent loop")
                    apply_field_overlays(body[sub], sov, f"{nid}.{sub}")
        else:
            ov = top_ov.get(nid, {})
            if "node.yaml" not in ov:
                raise LiteError(f"{nid}: not in the parent and no {nid}.node.yaml overlay")
            node = yaml.safe_load(ov["node.yaml"])
            if not isinstance(node, dict) or node.get("id") != nid:
                raise LiteError(f"{nid}.node.yaml must be a mapping with id: {nid}")
            if nid in loop_ov or any(k != "node.yaml" for k in ov):
                raise LiteError(f"{nid}: a new node takes only its node.yaml")
        if nid in dep_over:
            node["depends_on"] = list(dep_over[nid])
        # ports apply to retained and new nodes alike (new overlays may quote them too)
        node = substitute_ports(node, ports)
        out_nodes.append(node)

    # dangling depends_on
    ids = set(order)
    for n in out_nodes:
        for d in n.get("depends_on") or []:
            if d not in ids:
                raise LiteError(f"{n['id']}: depends_on '{d}' is not a selected node")

    # contracts: every selected node declared; every consumed artifact produced
    contracts = m["contracts"]
    missing = [nid for nid in order if nid not in contracts]
    if missing:
        raise LiteError(f"contracts missing for selected nodes: {', '.join(missing)}")
    extra = [nid for nid in contracts if nid not in ids]
    if extra:
        raise LiteError(f"contracts declared for unselected nodes: {', '.join(extra)}")
    produced = set(m.get("inherited_dynamic", []))
    for nid in order:
        produced.update(contracts[nid].get("produces", []))
    for nid in order:
        for art in contracts[nid].get("consumes", []):
            if art not in produced:
                raise LiteError(f"{nid} consumes '{art}' but no selected node produces it (and it is not inherited_dynamic)")

    doc = {"name": m["name"], "description": m["description"]}
    for k, v in parent.items():
        if k in ("name", "description", "nodes"):
            continue
        doc[k] = v
    doc["nodes"] = out_nodes
    text = dump_yaml(doc)

    # grep lint — warning only
    seen = set()
    for n in out_nodes:
        blob = json.dumps(n)
        for art in re.findall(r"\$\{?ARTIFACTS_DIR\}?/([A-Za-z0-9_.-]+\.(?:json|txt|md|html))", blob):
            if art not in produced and art not in seen:
                seen.add(art)
                print(f"DERIVE_LITE=WARN {n['id']} references {art} which no contract produces (grep lint; verify by hand)", file=sys.stderr)
    return m, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lane", choices=["api", "bugfix"])
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    m, text = derive(a.lane)
    target = a.out or os.path.join(ARCHON, "workflows", f"{m['name']}.yaml")
    if a.check:
        if not os.path.isfile(target):
            raise LiteError(f"shipped file missing: {target}")
        with open(target, encoding="utf-8") as fh:
            if fh.read() != text:
                print(f"LITE_DRIFT=FAIL {m['name']} (regenerate: python3 setup/derive-lite.py {a.lane})")
                sys.exit(1)
        print(f"LITE_DRIFT=OK {m['name']}")
        return
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"DERIVE_LITE=OK {m['name']} nodes={len(m['nodes'])} -> {os.path.relpath(target, ARCHON)}")


if __name__ == "__main__":
    main()
