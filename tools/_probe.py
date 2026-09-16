import tkinter as tk

r = tk.Tk()
print("tk version:", r.tk.call("info", "patchlevel"))
c = tk.Canvas(r, width=200, height=100)
c.pack()
c.create_rectangle(10, 10, 100, 50, fill="red")
r.update()
try:
    ps = c.postscript(colormode="color")
    print("canvas postscript OK, len:", len(ps))
    open(r"F:\C\Temp\probe.ps", "w").write(ps)
except Exception as e:
    print("canvas ps FAIL:", type(e).__name__, e)
r.destroy()
