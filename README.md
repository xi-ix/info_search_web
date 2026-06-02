# AI Radar

一个用于追踪 AI 论文、GitHub 项目和模型资讯的可视化小工具。

## 当前版本

- 提供一个可视化首页
- 展示三类信息卡片：论文、GitHub 项目、模型资讯
- 支持本地收藏，把感兴趣的链接保存到项目目录
- 支持按配置限制每一类展示数量
- 论文栏目已经接入 arXiv RSS 实时抓取
- 论文栏目在 RSS 失败时会自动尝试 arXiv API
- GitHub 项目已经接入 GitHub Search API
- 模型资讯已经接入多源 RSS/Atom 聚合

## 运行方式
python 3.9+
```bash
# windows
pip install -r requirements.txt
python -m src.app
# macos
pip3 install -r requirements.txt
python3 -m src.app
```
```


打开浏览器访问：
```
http://127.0.0.1:5000
```
配置示例
项目根目录下的 config.json 用来控制抓取范围、超时、来源和展示数量，例如：
```json
{
  "refresh_timeout_seconds": 8,
  "recent_days": 7,
  "arxiv": {
    "feeds": [
      "https://export.arxiv.org/rss/cs.AI",
      "https://export.arxiv.org/rss/cs.LG",
      "https://export.arxiv.org/rss/cs.CL"
    ],
    "max_items": 30,
    "min_results": 8,
    "api_max_results": 20,
    "proxy": "",
    "timeout_seconds": 10,
    "keywords": ["llm", "large language model", "agent", "rag", "multimodal", "reasoning"]
  },
  "github": {
    "max_items": 20,
    "min_stars": 200,
    "sort": "updated",
    "order": "desc",
    "timeout_seconds": 10,
    "proxy": "",
    "token": "",
    "keywords": ["llm", "agent", "rag", "multimodal", "reasoning"]
  },
  "news": {
    "feeds": [
      "https://openai.com/news/rss.xml",
      "https://huggingface.co/blog/feed.xml",
      "https://about.fb.com/news/category/product-news/feed/",
      "https://qwenlm.github.io/blog/index.xml"
    ],
    "html_sources": [
      {
        "name": "Hugging Face Blog",
        "kind": "huggingface_blog",
        "url": "https://huggingface.co/blog"
      },
      {
        "name": "Meta Newsroom",
        "kind": "meta_newsroom",
        "url": "https://about.fb.com/news/"
      },
      {
        "name": "DeepSeek News",
        "kind": "deepseek_news",
        "url": "https://api-docs.deepseek.com/updates/"
      }
    ],
    "max_items": 20,
    "recent_days": 10,
    "timeout_seconds": 4,
    "proxy": "",
    "keywords": ["model", "llm", "agent", "reasoning", "multimodal", "release", "api"]
  },
  "translation": {
    "enabled": true,
    "backend": "mymemory",
    "base_url": "https://api.mymemory.translated.net",
    "model": "",
    "api_key": "",
    "timeout_seconds": 20,
    "batch_size": 5,
    "source_lang": "en",
    "target_lang": "zh-CN",
    "email": ""
  },
  "limits": {
    "papers": 10,
    "projects": 10,
    "news": 30
  }
}
```
配置说明
```
refresh_timeout_seconds：整次页面刷新允许花费的最大总时间。

recent_days：默认只看最近多少天的内容。

arxiv.feeds：要抓取的 arXiv 分类。

arxiv.keywords：论文筛选关键词。

arxiv.timeout_seconds：单次 arXiv 请求超时。

arxiv.proxy：可选代理，例如 http://127.0.0.1:7890。

github.max_items：GitHub 项目最多保留多少条。

github.min_stars：最低 star 数要求。

github.token：可选 GitHub Personal Access Token，用来降低限流风险。

github.proxy：GitHub 请求代理。

news.feeds：RSS/Atom 资讯源。

news.html_sources：没有稳定 RSS 时的页面抓取源。

news.recent_days：资讯只保留最近多少天。

news.timeout_seconds：单次资讯请求超时。

news.proxy：资讯请求代理。

```
收藏保存位置
```
data/favorites.json
```
说明
```
Qwen 默认走它自己的 RSS。
OpenAI 默认只走 RSS，因为新闻页直接抓取容易返回 403。
Anthropic 当前默认没有加入，因为公开 RSS 不稳定；如果需要，建议单独做网页抓取。
后续建议
接入更多国内模型资讯源
增加关键词过滤与订阅分组
增加日报导出
增加收藏标签和备注

```