// About page: apply saved theme (so dark-mode users stay consistent) + wire
// the search box to redirect home. 独立成文件而非 about.html 内联，是为了让
// stamp_assets() 能给 <script src> 和这里的 import 正常打 ?v= 缓存戳。
import { Theme } from './storage.js?v=fd0e13f6';
import { attachSearchRedirect } from './utils.js?v=fd0e13f6';

Theme.init();
attachSearchRedirect();
