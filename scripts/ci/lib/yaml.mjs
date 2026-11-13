// scripts/ci/lib/yaml.mjs — 无依赖 YAML 子集解析器，只覆盖 GitHub Actions 工作流用到的写法：
// 块映射、块序列、行内序列 [a, b]、单双引号标量、块标量 | 和 >、注释、空行。
// 不支持的 YAML 1.1 隐式类型（on/off/yes/no）一律按字符串保留 —— 这正是我们要的。
// 返回 { value, errors }，errors 带行号，解析不完整也会收集到错误而不是抛异常。
export function parseYaml(text, label = "yaml") {
  const errors = [];
  const rawLines = String(text).replace(/^\uFEFF/, "").split(/\r?\n/);

  // 第一遍：去注释、折叠块标量，生成 {indent, content, line} 列表。
  const lines = [];
  for (let i = 0; i < rawLines.length; i += 1) {
    const raw = rawLines[i];
    if (!raw.trim() || /^\s*#/.test(raw)) continue;
    if (/\t/.test(raw.slice(0, raw.length - raw.trimStart().length))) {
      errors.push({ line: i + 1, message: "缩进里出现 Tab，YAML 不允许" });
    }
    const stripped = stripComment(raw);
    if (!stripped.trim()) continue;
    const indent = stripped.length - stripped.trimStart().length;
    // content 必须去掉行首缩进（indent 单独记账），否则 isSeq / 映射键都会带上前导空格。
    let content = stripped.trim();

    const block = content.match(/^(.*[:\-\s])?([|>])([+-]?)\s*$/);
    if (block && !/^\s*#/.test(content)) {
      // 收集后续更深缩进的行作为标量正文。
      const body = [];
      let j = i + 1;
      let bodyIndent = -1;
      for (; j < rawLines.length; j += 1) {
        const l = rawLines[j];
        if (!l.trim()) {
          body.push("");
          continue;
        }
        const ind = l.length - l.trimStart().length;
        if (ind <= indent) break;
        if (bodyIndent < 0) bodyIndent = ind;
        body.push(l.slice(Math.min(bodyIndent, ind)));
      }
      i = j - 1;
      const folded = block[3] === ">" ? body.join(" ").replace(/[ \t]+/g, " ").trim() : body.join("\n");
      lines.push({ indent, content: `${block[1] || ""}${JSON.stringify(folded)}`, line: i + 1, synthetic: true });
      continue;
    }
    lines.push({ indent, content, line: i + 1 });
  }

  let pos = 0;
  const peek = () => lines[pos];
  const isSeq = (l) => l && (l.content === "-" || l.content.startsWith("- "));

  function parseBlock(indent) {
    const first = peek();
    if (!first) return null;
    if (isSeq(first) && first.indent === indent) return parseSeq(indent);
    return parseMap(indent);
  }

  function parseMap(indent) {
    const obj = {};
    while (pos < lines.length) {
      const l = peek();
      if (l.indent !== indent) {
        if (l.indent < indent) break;
        errors.push({ line: l.line, message: `缩进异常：期望 ${indent}，实际 ${l.indent}` });
        pos += 1;
        continue;
      }
      if (isSeq(l)) break;
      const m = l.content.match(/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:]+?)\s*:\s*(.*)$/);
      if (!m) {
        errors.push({ line: l.line, message: `无法解析的映射行：${l.content.slice(0, 60)}` });
        pos += 1;
        continue;
      }
      const key = unquote(m[1].trim());
      const rest = m[2].trim();
      pos += 1;
      if (rest === "") {
        const nxt = peek();
        if (nxt && nxt.indent > indent) obj[key] = parseBlock(nxt.indent);
        else if (nxt && isSeq(nxt) && nxt.indent === indent) obj[key] = parseSeq(indent);
        else obj[key] = null;
      } else {
        obj[key] = parseScalar(rest, l.line);
      }
      if (Object.prototype.hasOwnProperty.call(obj, key)) {
        const dupes = Object.keys(obj).filter((k) => k === key).length;
        if (dupes > 1) errors.push({ line: l.line, message: `重复键 ${key}` });
      }
    }
    return obj;
  }

  function parseSeq(indent) {
    const arr = [];
    while (pos < lines.length) {
      const l = peek();
      if (l.indent !== indent || !isSeq(l)) break;
      const rest = l.content === "-" ? "" : l.content.slice(2).trim();
      pos += 1;
      if (rest === "") {
        const nxt = peek();
        arr.push(nxt && nxt.indent > indent ? parseBlock(nxt.indent) : null);
        continue;
      }
      // "- key: value" 形式：把这一行当作虚拟缩进为 indent+2 的映射首行。
      if (/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:]+?)\s*:\s*/.test(rest)) {
        const virtualIndent = indent + 2;
        const m = rest.match(/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:]+?)\s*:\s*(.*)$/);
        const item = {};
        const key = unquote(m[1].trim());
        const val = m[2].trim();
        if (val === "") {
          const nxt = peek();
          item[key] = nxt && nxt.indent > virtualIndent ? parseBlock(nxt.indent) : null;
        } else item[key] = parseScalar(val, l.line);
        while (pos < lines.length) {
          const nx = peek();
          if (nx.indent !== virtualIndent || isSeq(nx)) break;
          const mm = nx.content.match(/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^:]+?)\s*:\s*(.*)$/);
          if (!mm) {
            errors.push({ line: nx.line, message: `序列项内无法解析：${nx.content.slice(0, 60)}` });
            pos += 1;
            continue;
          }
          pos += 1;
          const k2 = unquote(mm[1].trim());
          const v2 = mm[2].trim();
          if (v2 === "") {
            const nn = peek();
            item[k2] = nn && nn.indent > virtualIndent ? parseBlock(nn.indent) : null;
          } else item[k2] = parseScalar(v2, nx.line);
        }
        arr.push(item);
      } else {
        arr.push(parseScalar(rest, l.line));
      }
    }
    return arr;
  }

  function parseScalar(s, line) {
    if (s.startsWith("[") && s.endsWith("]")) {
      const inner = s.slice(1, -1).trim();
      if (!inner) return [];
      return splitFlow(inner).map((x) => parseScalar(x.trim(), line));
    }
    if (s.startsWith("{") && s.endsWith("}")) {
      const obj = {};
      for (const part of splitFlow(s.slice(1, -1))) {
        const idx = part.indexOf(":");
        if (idx < 0) continue;
        obj[unquote(part.slice(0, idx).trim())] = parseScalar(part.slice(idx + 1).trim(), line);
      }
      return obj;
    }
    return coerce(unquote(s), s);
  }

  const value = parseBlock(lines.length ? lines[0].indent : 0);
  if (lines.length && pos < lines.length) {
    errors.push({ line: lines[pos].line, message: `解析未消费完，剩余 ${lines.length - pos} 行` });
  }
  return { value, errors, label };
}

// 只做必要类型还原：true/false/null/整数/小数；引号内的值保持字符串，on/off/yes/no 不做 YAML 1.1 布尔化。
function coerce(value, raw) {
  if (value !== raw) return value; // 带引号 → 字符串
  if (/^(true|false)$/i.test(value)) return value.toLowerCase() === "true";
  if (/^(null|~)$/i.test(value)) return null;
  if (/^-?\d+$/.test(value)) return Number(value);
  if (/^-?\d*\.\d+$/.test(value)) return Number(value);
  return value;
}

function splitFlow(s) {
  const out = [];
  let buf = "";
  let quote = null;
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (quote) {
      buf += ch;
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      buf += ch;
    } else if (ch === ",") {
      out.push(buf);
      buf = "";
    } else buf += ch;
  }
  if (buf.trim()) out.push(buf);
  return out;
}

export function stripComment(line) {
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      continue;
    }
    if (ch === "#" && (i === 0 || /\s/.test(line[i - 1]))) return line.slice(0, i);
  }
  return line;
}

export function unquote(s) {
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    try {
      return JSON.parse(s);
    } catch {
      return s.slice(1, -1);
    }
  }
  if (s.length >= 2 && s.startsWith("'") && s.endsWith("'")) {
    return s.slice(1, -1).replace(/''/g, "'");
  }
  return s;
}
