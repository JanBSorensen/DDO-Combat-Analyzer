"""Safe append-only focus-resume recovery; never guess scrolled-out log rows."""
from __future__ import annotations
import numpy as np


def recover_append_rows(prev_feat, curr_gray, curr_feat, prev_phase, curr_phase,
                        line_height, *, scale=1.0, max_new_rows=4):
    if prev_feat is None or prev_phase is None:
        return []
    if prev_feat.shape != curr_feat.shape or curr_feat.shape != curr_gray.shape:
        return []
    lh = int(line_height)
    if lh <= 0:
        return []
    # Only unchanged grid or an innocent one-pixel phase shift.
    delta = abs(int(curr_phase)-int(prev_phase)) % lh
    if min(delta,lh-delta) > 1:
        return []
    cells=[]
    y=int(curr_phase)
    while y<0: y+=lh
    while y+lh<=curr_gray.shape[0]:
        cells.append((y,y+lh))
        y+=lh
    if len(cells)<8:
        return []
    old=[float(np.mean(prev_feat[y0:y1]))/scale for y0,y1 in cells]
    new=[float(np.mean(curr_feat[y0:y1]))/scale for y0,y1 in cells]
    # This can only establish safety while the bottom of the Combat Log
    # is still empty.  Once scrolling may have happened, discard and rebase.
    if any(x>=0.002 for x in old[-3:]+new[-3:]):
        return []
    if any(p>=0.008 and c<0.003 for p,c in zip(old,new)):
        return []
    gained=[i for i,(p,c) in enumerate(zip(old,new)) if p<0.002 and c>=0.003]
    if not gained or len(gained)>max_new_rows:
        return []
    # No holes or non-append reordering; all recovered rows must follow the
    # previously occupied region and be consecutive.
    old_used=[i for i,v in enumerate(old) if v>=0.003]
    if old_used and min(gained)<=max(old_used):
        return []
    if gained != list(range(gained[0],gained[-1]+1)):
        return []
    return [curr_gray[cells[i][0]:cells[i][1]].copy() for i in gained]
