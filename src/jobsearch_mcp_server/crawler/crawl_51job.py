"""
前程无忧 (51job) 职位爬虫（统一版）
====================================
使用 Selenium 控制 Chrome 浏览器，抓取指定城市、关键词的职位数据。
支持多城市、多关键词、自定义页数，结果保存为 JSON 格式。

城市代码:
    090200 = 上海, 090000 = 成都, 010000 = 北京
    020000 = 广州, 030000 = 深圳, 070000 = 杭州
    190000 = 南京, 200000 = 武汉, 210000 = 西安
"""

import json
import os
import random
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
#  城市代码映射
# ============================================================
CITY_CODES = {
    "上海": "090200",
    "成都": "090000",
    "北京": "010000",
    "广州": "020000",
    "深圳": "030000",
    "杭州": "070000",
    "南京": "190000",
    "武汉": "200000",
    "西安": "210000",
}


# ============================================================
#  爬虫主函数
# ============================================================
def fetch_51job(
    keyword: str = "AI应用开发",
    city: str = "上海",
    max_pages: int = 2,
    headless: bool = True,
    output_dir: str | None = None,
) -> list[dict]:
    """
    抓取前程无忧指定地区、关键词的职位信息

    参数:
        keyword:    搜索关键词（如 "AI应用开发"、"Python后端"）
        city:       城市名称（如 "上海"、"成都"），见 CITY_CODES
        max_pages:  抓取页数，默认 2 页
        headless:   是否使用无头模式，默认 True
        output_dir: 输出目录，指定后自动保存 JSON 文件

    返回:
        list[dict]，每个 dict 包含职位详细信息
    """
    city_code = CITY_CODES.get(city, "090200")
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    driver = None
    all_jobs: list[dict] = []

    try:
        print(f"[浏览器] 正在初始化浏览器（{'无头模式' if headless else '有界面模式'}）...")
        # 优先使用本地缓存的 ChromeDriver，避免每次联网检查版本更新
        import glob

        cached_drivers = glob.glob(
            os.path.expanduser(
                r"~\.wdm\drivers\chromedriver\win64\*\chromedriver-win32\chromedriver.exe"
            )
        )
        if cached_drivers:
            # 使用最新缓存的版本
            chrome_driver_path = max(cached_drivers, key=os.path.getmtime)
            print(f"[浏览器] 使用本地缓存 ChromeDriver: {chrome_driver_path}")
            service = Service(chrome_driver_path)
        else:
            print("[浏览器] 未找到本地缓存，尝试在线下载...")
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)

        search_url = (
            f"https://we.51job.com/pc/search?keyword={keyword}&jobArea={city_code}&searchType=2"
        )
        print(f"[搜索] 正在搜索: {keyword} | 城市: {city} | URL: {search_url}")
        driver.get(search_url)

        # 等待职位列表加载
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".joblist-item"))
            )
            print("[成功] 职位列表加载成功")
        except Exception:
            print("[警告] 等待职位列表超时，可能触发了反爬或需要登录")
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            print("[保存] 已保存页面源码到 debug_page.html")
            return all_jobs

        for page in range(1, max_pages + 1):
            print(f"\n{'=' * 50}")
            print(f"[页面] 正在抓取第 {page}/{max_pages} 页")
            print(f"{'=' * 50}")

            time.sleep(random.uniform(2, 4))

            job_cards = driver.find_elements(By.CSS_SELECTOR, ".joblist-item")
            print(f"  本页找到 {len(job_cards)} 个职位")

            for idx, card in enumerate(job_cards):
                try:

                    def safe_text(selector, default="", _card=card):
                        try:
                            el = _card.find_element(By.CSS_SELECTOR, selector)
                            return el.text.strip()
                        except Exception:
                            return default

                    job = {
                        "序号": (page - 1) * len(job_cards) + idx + 1,
                        "职位名称": safe_text(".jname"),
                        "公司名称": safe_text(".cname"),
                        "工作地点": safe_text(".area .shrink-0"),
                        "薪资范围": safe_text(".sal"),
                        "经验学历": safe_text(".dc.shrink-0"),
                        "公司性质": safe_text(".dc.shrink-0"),
                        "来源": "51job",
                        "城市": city,
                        "关键词": keyword,
                        "抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    all_jobs.append(job)
                except Exception as e:
                    print(f"  [警告] 解析第 {idx + 1} 个卡片出错: {e}")
                    continue

            # 翻页逻辑
            if page < max_pages:
                print(f"  尝试翻到第 {page + 1} 页...")
                try:
                    pagination_selectors = [
                        ".pagination li",
                        ".page-index li",
                        ".pagination .number",
                        "li[class*='number']",
                        ".pagination li:not(.next):not(.prev)",
                    ]

                    page_links = []
                    for sel in pagination_selectors:
                        page_links = driver.find_elements(By.CSS_SELECTOR, sel)
                        if page_links:
                            break

                    if not page_links:
                        next_btn = driver.find_elements(
                            By.CSS_SELECTOR,
                            ".pagination .next, .page-index .next, a[class*='next']",
                        )
                        if next_btn:
                            driver.execute_script("arguments[0].click();", next_btn[0])
                            print("  [成功] 已点击'下一页'按钮")
                            time.sleep(random.uniform(3, 5))
                            continue
                        else:
                            print("  [警告] 翻页元素未找到，停止翻页")
                            break

                    numeric_btns = []
                    for btn in page_links:
                        txt = btn.text.strip()
                        if txt.isdigit():
                            numeric_btns.append((int(txt), btn))

                    if not numeric_btns:
                        print("  [警告] 未找到数字页码按钮")
                        break

                    target_page = page + 1
                    target_btn = None
                    for num, btn in numeric_btns:
                        if num == target_page:
                            target_btn = btn
                            break

                    if target_btn is None:
                        print(f"  [警告] 未找到第 {target_page} 页按钮")
                        break

                    driver.execute_script("arguments[0].scrollIntoView(true);", target_btn)
                    time.sleep(0.5)
                    try:
                        target_btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", target_btn)

                    print(f"  [成功] 已点击第 {target_page} 页，等待加载...")
                    time.sleep(random.uniform(3, 5))

                except Exception as e:
                    print(f"  [警告] 翻页失败: {e}")
                    break

    except Exception as e:
        print(f"[失败] 爬虫运行出错: {e}")

    finally:
        if driver:
            print("\n[关闭] 正在关闭浏览器...")
            driver.quit()

    # 自动保存结果
    if output_dir and all_jobs:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"51job_{keyword}_{city}_{timestamp}.json"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(all_jobs, f, ensure_ascii=False, indent=2)
        print(f"[保存] 已保存 {len(all_jobs)} 个职位到 {filepath}")

    print(f"\n[统计] 共抓取 {len(all_jobs)} 个职位")
    return all_jobs


def format_jobs_text(jobs: list[dict]) -> str:
    """将职位列表格式化为可读文本（用于 LLM 匹配）"""
    if not jobs:
        return "暂无职位数据。"

    lines = []
    for job in jobs:
        lines.append(f"{job['序号']}. 岗位名称: {job['职位名称']}")
        lines.append(f"   公司名称: {job['公司名称']}")
        lines.append(f"   工作地点: {job['工作地点']}")
        lines.append(f"   薪资范围: {job['薪资范围']}")
        lines.append(f"   经验学历: {job['经验学历']}")
        lines.append(f"   公司性质: {job['公司性质']}")
        lines.append("")
    return "\n".join(lines)


# ============================================================
#  独立运行测试
# ============================================================
if __name__ == "__main__":
    import sys

    keyword = sys.argv[1] if len(sys.argv) > 1 else "AI应用开发"
    city = sys.argv[2] if len(sys.argv) > 2 else "上海"
    pages = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    jobs = fetch_51job(
        keyword=keyword,
        city=city,
        max_pages=pages,
        headless=True,
        output_dir=".",
    )

    print("\n" + "=" * 70)
    print(format_jobs_text(jobs))
