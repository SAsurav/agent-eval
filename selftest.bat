@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (set PY=E:\Anaconda\python.exe) else (set PY=python)

echo ============================================================
echo   自检（不需要 API key）
echo   验证：语料加载 / 用例生成 / 评测判定逻辑
echo ============================================================
echo.

echo [1/3] 加载语料并建检索索引...
"%PY%" -c "import retrieval; c=retrieval.load_corpus(r'corpus'); b=retrieval.BM25(c); print('  语料', len(c), '块'); [print('  命中:', h['id'], '|', h['score']) for h in b.search('赞助商推荐 要不要收费', 3)]"
echo.

echo [2/3] 生成评测用例...
"%PY%" make_cases.py --corpus corpus --out evals\cases_v1.jsonl
echo.

echo [3/3] 跑 mock 评测（验证判定逻辑，不是真实模型分数）...
"%PY%" eval.py --mock --cases evals\cases_v1.jsonl --out evals\mock_baseline.json
echo.

echo ============================================================
echo 自检完成。上面的通过率是 mock 的，不代表真实模型水平。
echo 要跑真实 baseline：在本目录建 key.txt 写一行 DeepSeek key，然后运行
echo    %PY% eval.py --out evals\baseline.json
echo ============================================================
pause
