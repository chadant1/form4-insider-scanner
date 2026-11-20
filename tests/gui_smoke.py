"""Manual GUI smoke test (Linux + Xvfb). Not run in CI."""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
_src = open(os.path.join(HERE, "test_scanner.py")).read()
exec(_src.split("# 1. Market-wide default")[0])                     # fixtures + mocked SEC
_h = _src.split("HIST = 424242", 1)[1].split("CALLS.clear()")[0]   # history fixtures (company HIST)
exec("HIST = 424242" + _h)
import tkinter as tk
import webbrowser
opened = []
webbrowser.open = lambda url, *a, **k: opened.append(url)
_orig = F.SecClient.get
F.SecClient.get = lambda self, url, retries=4: (time.sleep(0.15), _orig(self, url, retries))[1]
orig_mainloop = tk.Tk.mainloop

def shot(name):
    subprocess.run(["import", "-window", "root", os.path.join(OUT, name + ".png")])

def mainloop(self, n=0):
    import gc
    def go():
        app = next(o for o in gc.get_objects() if type(o).__name__ == "App")
        app.ua_var.set("Jane Doe jane@example.com")
        app.apply_preset()
        print("PRESET:", app.status_var.get())
        app.min_var.set("0"); app.code_vars["S"].set(True)
        app.tickers_var.set("HIST, NVDA, MSFT, AAPL")
        app.cluster_only_var.set(False); app.min_stake_var.set("0"); app.excl_plan_var.set(False)
        app.hide_routine_var.set(False); app.hide_sold_var.set(False)
        self.state("normal"); self.geometry("1900x950+0+0")
        app._view_changed()
        app.start_scan()
        self.after(14000, lambda: step2(app))
    def step2(app):
        app.sort_by("pattern")
        self.update(); shot("1_all_rows")
        print("PATTERNS:", sorted((r["insider"], r["pattern"], r["sold"]) for r in app.rows.values() if r["code"] == "P"))
        print("ALL:", app.summary_var.get())
        app.hide_routine_var.set(True); app.hide_sold_var.set(True); app._view_changed()
        self.after(400, lambda: step2b(app))
    def step2b(app):
        self.update(); shot("1b_hide_routine_sold")
        print("HIDE ROUTINE+SOLD:", sorted(app.rows[i]["insider"] for i in app.tree.get_children()))
        app.hide_routine_var.set(False); app.hide_sold_var.set(False)
        app.excl_plan_var.set(True); app.cluster_only_var.set(True); app._view_changed()
        self.after(400, lambda: step3(app))
    def step3(app):
        self.update(); shot("2_clusters_only")
        vis = [app.rows[i] for i in app.tree.get_children()]
        print("CLUSTERS-ONLY:", [(r["ticker"], r["insider"], r["cluster"], r["plan"]) for r in vis])
        app.cluster_only_var.set(False); app.min_stake_var.set("20"); app._view_changed()
        self.after(400, lambda: step4(app))
    def step4(app):
        vis = [app.rows[i] for i in app.tree.get_children()]
        print("STAKE>=20:", sorted((r["ticker"], F.fmt_stake(r["stake"])) for r in vis))
        app.sort_by("stake")
        print("SORT STAKE:", [F.fmt_stake(app.rows[i]["stake"]) for i in app.tree.get_children()])
        iid = app.tree.get_children()[0]; app.tree.selection_set(iid); app.filter_to_ticker()
        print("ONE TICKER:", {app.rows[i]["ticker"] for i in app.tree.get_children()}, app.summary_var.get())
        self.after(300, self.destroy)
    self.after(800, go)
    orig_mainloop(self, n)

tk.Tk.mainloop = mainloop
F.run_gui()
