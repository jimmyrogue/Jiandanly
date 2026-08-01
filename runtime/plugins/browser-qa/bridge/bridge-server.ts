import readline from "node:readline";
import { chromium, type BrowserContext, type Locator, type Page } from "playwright";

const profile = process.env.SHEJANE_BROWSER_QA_PROFILE;
if (!profile) throw new Error("SHEJANE_BROWSER_QA_PROFILE is required");

let context: BrowserContext | undefined;
let page: Page | undefined;
let state = 0;
let stateId = "";
let refs = new Map<string, Locator>();
let closing = false;
const consoleMessages: Array<{ type: string; text: string }> = [];
const networkEvents: Array<{ method: string; url: string; status?: number; failure?: string }> = [];

function bounded(value: string, limit: number) {
  return value.length <= limit ? value : `${value.slice(0, limit)}…`;
}

function nextState() {
  state += 1;
  stateId = `state-${state}`;
  return stateId;
}

async function ensurePage() {
  if (page && !page.isClosed()) return page;
  const proxy = process.env.SHEJANE_BROWSER_QA_PROXY;
  context = await chromium.launchPersistentContext(profile!, {
    channel: "chromium",
    headless: process.env.SHEJANE_BROWSER_QA_HEADLESS !== "0",
    proxy: proxy ? { server: proxy, bypass: "<-loopback>" } : undefined,
    viewport: { width: 1280, height: 900 },
  });
  page = context.pages()[0] ?? await context.newPage();
  page.on("console", message => {
    consoleMessages.push({ type: message.type(), text: bounded(message.text(), 2000) });
    if (consoleMessages.length > 100) consoleMessages.shift();
  });
  page.on("requestfailed", request => {
    networkEvents.push({
      method: request.method(),
      url: bounded(request.url(), 2048),
      failure: bounded(request.failure()?.errorText ?? "failed", 500),
    });
    if (networkEvents.length > 100) networkEvents.shift();
  });
  page.on("response", response => {
    if (response.status() < 400) return;
    networkEvents.push({
      method: response.request().method(),
      url: bounded(response.url(), 2048),
      status: response.status(),
    });
    if (networkEvents.length > 100) networkEvents.shift();
  });
  nextState();
  return page;
}

async function snapshot() {
  const active = await ensurePage();
  const candidates = active.locator(
    "a,button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=checkbox],[role=radio],[tabindex]",
  );
  const count = Math.min(await candidates.count(), 200);
  refs = new Map();
  const elements: Array<{ ref: string; role: string; name: string }> = [];
  for (let index = 0; index < count; index += 1) {
    const locator = candidates.nth(index);
    if (!await locator.isVisible().catch(() => false)) continue;
    const identity = await locator.evaluate((element: Element) => {
      const html = element as HTMLElement;
      const input = element as HTMLInputElement;
      const tag = element.tagName.toLowerCase();
      const role = element.getAttribute("role") || ({ a: "link", button: "button", input: input.type || "input", textarea: "textbox", select: "select" } as Record<string, string>)[tag] || tag;
      const name = element.getAttribute("aria-label") || element.getAttribute("title") || input.placeholder || html.innerText || element.textContent || "";
      return { role, name: name.replace(/\s+/g, " ").trim() };
    }).catch(() => ({ role: "element", name: "" }));
    const ref = `e${elements.length + 1}`;
    refs.set(ref, locator);
    elements.push({ ref, role: bounded(identity.role, 80), name: bounded(identity.name, 300) });
  }
  stateId = nextState();
  return {
    state_id: stateId,
    url: bounded(active.url(), 2048),
    title: bounded(await active.title(), 500),
    text: bounded(await active.locator("body").innerText().catch(() => ""), 20_000),
    elements,
  };
}

async function invoke(action: string, args: Record<string, unknown>) {
  if (action === "open") {
    const url = new URL(String(args.url ?? ""));
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      throw new Error("Browser QA accepts only credential-free HTTP or HTTPS URLs");
    }
    const active = await ensurePage();
    await active.goto(url.toString(), { waitUntil: "domcontentloaded", timeout: 45_000 });
    return await snapshot();
  }
  if (action === "observe") return await snapshot();
  if (action === "act") {
    const active = await ensurePage();
    if (args.state_id !== stateId) throw new Error("Browser QA page state changed; observe again");
    const kind = String(args.action ?? "");
    const value = String(args.value ?? "");
    if (kind === "scroll") {
      const amount = Number.parseInt(value, 10);
      if (!Number.isFinite(amount) || Math.abs(amount) > 5000) throw new Error("scroll must be between -5000 and 5000");
      await active.mouse.wheel(0, amount);
    } else {
      const locator = refs.get(String(args.ref ?? ""));
      if (!locator) throw new Error("Browser QA ref is missing or stale; observe again");
      if (kind === "click") await locator.click();
      else if (kind === "fill") {
        if ((await locator.getAttribute("type"))?.toLowerCase() === "password") {
          throw new Error("Password entry must be completed by the user in the visible browser");
        }
        await locator.fill(value);
      } else if (kind === "select") await locator.selectOption(value);
      else if (kind === "press") await locator.press(value);
      else throw new Error(`Unknown Browser QA action: ${kind}`);
    }
    await active.waitForTimeout(100);
    return await snapshot();
  }
  if (action === "inspect") {
    const active = await ensurePage();
    const kind = String(args.kind ?? "");
    if (kind === "console") return { state_id: stateId, url: active.url(), title: await active.title(), console: consoleMessages };
    if (kind === "network") return { state_id: stateId, url: active.url(), title: await active.title(), network: networkEvents };
    if (kind === "screenshot") {
      const base64 = await active.screenshot({ type: "png", fullPage: false }).then(data => data.toString("base64"));
      return { state_id: stateId, url: active.url(), title: await active.title(), images: [{ base64, mime_type: "image/png" }] };
    }
    throw new Error(`Unknown Browser QA inspection: ${kind}`);
  }
  if (action === "close") {
    await context?.close();
    context = undefined;
    page = undefined;
    refs = new Map();
    stateId = "";
    return { closed: true };
  }
  throw new Error(`Unknown Browser QA action: ${action}`);
}

async function shutdown() {
  if (closing) return;
  closing = true;
  await context?.close().catch(() => undefined);
  process.exit(0);
}

process.on("SIGTERM", () => void shutdown());
process.on("SIGINT", () => void shutdown());

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let id: unknown;
  try {
    const request = JSON.parse(line);
    id = request.id;
    const result = await invoke(String(request.action), request.arguments || {});
    process.stdout.write(`${JSON.stringify({ id, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id, error: { message: error instanceof Error ? error.message : String(error) } })}\n`);
  }
}
await context?.close();
