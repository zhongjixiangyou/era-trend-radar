import { defineConfig } from 'astro/config';

// GitHub Pages 项目站点：https://zhongjixiangyou.github.io/era-trend-radar/
export default defineConfig({
  site: 'https://zhongjixiangyou.github.io',
  base: '/era-trend-radar',
  trailingSlash: 'always',
  build: { format: 'directory' },
  devToolbar: { enabled: false },
});
