"""Manual GUI smoke test (Linux + Xvfb). Not run in CI."""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
_src = open(os.path.join(HERE, "test_scanner.py")).read()
exec(_src.split("# 1. Market-wide default")[0])                     # fixtures + mocked SEC
_h = _src.split("HIST = 424242", 1)[1].split("CALLS.clear()")[0]   # history fixtures (company HIST)
exec("HIST = 424242" + _h)
_g = _src.split("SIG = 777001", 1)[1].split("F.Cache.clear_all()")[0]   # v1.3 fixtures (company SIGL)
exec("SIG = 777001" + _g)
exec("ERN = 888101" + _src.split("ERN = 888101", 1)[1].split("F.Cache.clear_all()")[0])      # v1.4 live fixtures
exec("import io as _io" + _src.split("import io as _io", 1)[1].split("CALLS.clear()\nbq_")[0])  # back-test fixtures
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
        app.tickers_var.set("ERNC, SIGL, HIST, NVDA, MSFT")
        app.cluster_only_var.set(False); app.min_stake_var.set("0"); app.excl_plan_var.set(False)
        app.hide_routine_var.set(False); app.hide_sold_var.set(False)
        self.state("normal"); self.geometry("1900x950+0+0")
        app._view_changed()
        app.start_scan()
        self.after(18000, lambda: step2(app))
    def step2(app):
        app.sort_by("score"); app.sort_by("score") if not app.sort_desc else None
        self.update(); shot("1_all_rows")
        print("SCORES:", [(app.rows[i]["ticker"], app.rows[i]["insider"][:14], app.rows[i].get("score")) for i in app.tree.get_children()])
        app.tree.selection_set(app.tree.get_children()[0])
        r0 = app.rows[app.tree.get_children()[0]]
        print("WHY TOP:", r0["score"], r0["score_why"][:4])
        tr = None
        import gc as _gc
        app.show_track_record()
        self.after(2500, lambda: (self.update(), shot("4_track_record"),
                                  print("TRACK:", [w for w in _gc.get_objects() if type(w).__name__ == "TrackRecordWindow"][0].summary.get())))
        print("PATTERNS:", sorted((r["insider"], r["pattern"], r["sold"]) for r in app.rows.values() if r["code"] == "P"))
        print("ALL:", app.summary_var.get())
        app.hide_routine_var.set(True); app.hide_sold_var.set(True); app._view_changed()
        self.after(3500, lambda: step2b(app))
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
    def step_bt(app):
        app.show_backtest()
        bw = app._bt_win
        bw.start_var.set("2025Q1"); bw.end_var.set("2025Q2"); bw.warm_var.set(2)
        bw.min_var.set("50000"); bw.max_var.set("0")
        bw.run()
        def wait():
            if bw.result is None and str(bw.run_btn.cget("state")) == "disabled":
                self.after(500, wait)
                return
            self.update(); shot("5_backtest_bands")
            print("BT STATUS:", bw.status.get())
            print("BT SUMMARY:", bw.summary.get())
            nb = bw.t_factors.master.master
            nb.select(1); self.update(); shot("6_backtest_factors")
            nb.select(2); self.update(); shot("7_backtest_test")
            self.after(300, self.destroy)
        self.after(1000, wait)

    def step4(app):
        vis = [app.rows[i] for i in app.tree.get_children()]
        print("STAKE>=20:", sorted((r["ticker"], F.fmt_stake(r["stake"])) for r in vis))
        app.sort_by("stake")
        print("SORT STAKE:", [F.fmt_stake(app.rows[i]["stake"]) for i in app.tree.get_children()])
        iid = app.tree.get_children()[0]; app.tree.selection_set(iid); app.filter_to_ticker()
        print("ONE TICKER:", {app.rows[i]["ticker"] for i in app.tree.get_children()}, app.summary_var.get())
        app.clear_ticker_filter(); app.min_stake_var.set("0"); app._view_changed()
        self.after(300, lambda: step_bt(app))
    self.after(800, go)
    orig_mainloop(self, n)

tk.Tk.mainloop = mainloop
F.run_gui()
