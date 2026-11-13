const base = import.meta.env.BASE_URL.replace(/\/$/, '');

/** 站内链接：自动带上 GitHub Pages 的 base 前缀 */
export function href(path: string): string {
  if (!path.startsWith('/')) path = '/' + path;
  return `${base}${path}`;
}

export { base };
