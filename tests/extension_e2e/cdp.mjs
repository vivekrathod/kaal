// A very small Chrome DevTools Protocol client for the test run.
export class Page {
  constructor(url) { this.url = url; this.next = 1; this.waiting = new Map(); }
  async open() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => { this.socket.onopen = resolve; this.socket.onerror = reject; });
    this.socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      const pending = this.waiting.get(message.id);
      if (!pending) return;
      this.waiting.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    };
    return this;
  }
  send(method, params = {}) {
    const id = this.next++;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.waiting.delete(id); reject(new Error(`${method} got no answer`)); }, 20000);
      this.waiting.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    });
  }
  async run(expression) {
    const answer = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: false });
    if (answer.exceptionDetails) throw new Error(answer.exceptionDetails.exception?.description || answer.exceptionDetails.text);
    return answer.result.value;
  }
  async click(selector) {
    const box = await this.run(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.scrollIntoView({ block: 'center' }); const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
    for (const type of ["mousePressed", "mouseReleased"]) await this.send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  }
  close() { this.socket.close(); }
}
export const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export async function until(check, what, limit = 30000) {
  const started = Date.now();
  for (;;) {
    const value = await check().catch(() => null);  // a page still loading is simply not ready
    if (value) return value;
    if (Date.now() - started > limit) throw new Error(`Timed out waiting for ${what}`);
    await wait(250);
  }
}
export class Browser {
  constructor(port) { this.base = `http://127.0.0.1:${port}`; }
  async targets() { return (await fetch(`${this.base}/json/list`)).json(); }
  async newTab(url) {
    const target = await (await fetch(`${this.base}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
    const page = await new Page(target.webSocketDebuggerUrl).open();
    await until(() => page.run("document.readyState === 'complete' && location.href !== 'about:blank'"), `${url} to load`);
    await wait(300);
    return { target, page };
  }
  async closeTab(id) { await fetch(`${this.base}/json/close/${id}`); }
}
