"""Manual GUI smoke test (Linux + Xvfb): python tests/gui_smoke.py OUTPUT_DIR. Not run in CI."""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
_src = open(os.path.join(HERE, "test_scanner.py")).read()
exec(_src.split("# 1. Market-wide default")[0])                                           # fixtures + mocked SEC
for marker, end in (("HIST = 424242", "CALLS.clear()"), ("SIG = 777001", "F.Cache.clear_all()"),
                    ("ERN = 888101", "F.Cache.clear_all()"), ("SLC = 555777", "F.Cache.clear_all()")):
    exec(marker + _src.split(marker, 1)[1].split(end)[0])
exec("import io as _io" + _src.split("import io as _io", 1)[1].split("CALLS.clear()\nbq_")[0])  # back-test data

import tkinter as tk  # noqa: E402
import webbrowser  # noqa: E402
webbrowser.open = lambda url, *a, **k: None
_orig = F.SecClient.get
F.SecClient.get = lambda self, url, retries=4: (time.sleep(0.02), _orig(self, url, retries))[1]
orig_mainloop = tk.Tk.mainloop
log = []


def shot(name):
    subprocess.run(["import", "-window", "root", os.path.join(OUT, name + ".png")])


def mainloop(self, n=0):
    import gc

    def app_obj():
        return next(o for o in gc.get_objects() if type(o).__name__ == "App")

    def go():
        app = app_obj()
        self.state("normal")
        self.geometry("1900x980+0+0")
        print("SIGNED IN AS:", app.ua_var.get())
        app.apply_preset()
        app.min_var.set("0")
        app.code_vars["S"].set(True)
        app.tickers_var.set("SELC, ERNC, SIGL, HIST, NVDA, MSFT")
        for v in (app.cluster_only_var, app.hide_routine_var, app.hide_sold_var, app.excl_plan_var):
            v.set(False)
        app.min_stake_var.set("0")
        app.view_var.set("all")
        app.start_scan()
        wait_scan(app)

    def wait_scan(app):
        if str(app.start_btn.cget("state")) == "disabled":
            self.after(400, lambda: wait_scan(app))
            return
        self.after(500, lambda: views(app))

    def views(app):
        app.toggle_settings()
        app.view_var.set("all"); app._view_mode_changed(); self.update(); shot("1_light_all")
        res = {}
        for key, _ in F.VIEWS if hasattr(F, "VIEWS") else []:
            pass
        for key in ("top_buys", "top_sells", "clusters", "company", "insider", "largest"):
            app.view_var.set(key); app._view_mode_changed(); self.update()
            res[key] = (len(app._groups), len(app._displayed()))
            if key in ("top_sells", "clusters", "insider"):
                shot(f"2_view_{key}")
        print("VIEWS:", res)
        app.view_var.set("clusters"); app._view_mode_changed()
        g = app._groups[0] if app._groups else None
        if g:
            app.tree.selection_set(g)
            print("GROUP SEL ROWS:", len(app._sel()), "GROUP VALUES:", app.tree.item(g, "values")[:8])
        app.search_var.set("sell co"); app._apply_view(); self.update()
        print("SEARCH 'sell co':", len(app._displayed()))
        app.search_var.set("")
        print("CARDS:", {k: (c["value"].cget("text"), c["detail"].cget("text")) for k, c in app._cards_w.items()})
        app.view_var.set("top_sells"); app._view_mode_changed()
        top = app._displayed()[0]
        app.tree.selection_set(top)
        r = app.rows[top]
        print("TOP SELL:", r["ticker"], r["insider"], r["score"], r["score_why"][:3])
        themes(app, ["Dark", "Midnight Blue", "Sepia (warm)"])

    def themes(app, names):
        if not names:
            app.set_theme("Dark")
            app.view_var.set("company"); app._view_mode_changed()
            app.show_track_record()
            self.after(2500, lambda: (self.update(), shot("4_dark_track_record"), backtest(app)))
            return
        name = names.pop(0)
        app.view_var.set("all" if name != "Midnight Blue" else "top_buys"); app._view_mode_changed()
        app.set_theme(name)
        self.update()
        shot("3_theme_" + name.split()[0].lower())
        self.after(300, lambda: themes(app, names))

    def backtest(app):
        app.show_backtest()
        bw = app._bt_win
        bw.start_var.set("2025Q1"); bw.end_var.set("2025Q2"); bw.warm_var.set(2)
        bw.min_var.set("5000"); bw.max_var.set("0"); bw.side_var.set("S")
        bw.run()

        def wait():
            if bw.result is None and str(bw.run_btn.cget("state")) == "disabled":
                self.after(400, wait)
                return
            self.update(); shot("5_dark_backtest_sells")
            print("BT:", bw.status.get())
            print("BT SUMMARY:", bw.summary.get()[:160])
            self.after(300, self.destroy)
        self.after(800, wait)

    self.after(800, go)
    orig_mainloop(self, n)


tk.Tk.mainloop = mainloop
# An older version saved the owner's contact; the app must ignore it and use this session's sign-in.
F.save_settings({**F.load_settings(), "user_agent": "Owner owner@gmail.com"})
F.ask_sec_identity = lambda *a, **k: "Jane Doe jane@example.com"
F.run_gui()
print("SAVED CONTACT AFTER CLOSE:", F.load_settings().get("user_agent"))
