@echo off
cd /d C:\Projects\STRATEGY_TESTER

call venv\Scripts\activate

echo ==============================
echo Running tester
echo ==============================
python engine\tester.py > logs\daily_tester.log 2>&1

echo ==============================
echo Checking report
echo ==============================
python -c "import json; d=json.load(open('reports/gate_report_latest.json','r',encoding='utf-8')); print(d['meta']['generated']); print(d['meta']['nasdaq_regime']); print(len(d['stocks']))" >> logs\daily_tester.log 2>&1

echo ==============================
echo Uploading report to VPS
echo ==============================
scp "C:\Projects\STRATEGY_TESTER\reports\gate_report_latest.json" administrator@204.12.203.95:/app/reports/gate_report_latest.json

echo ==============================
echo Running VPS portfolio manager DRY RUN
echo ==============================
ssh administrator@204.12.203.95 "python3 /app/live_portfolio_manager.py --dry-run --max-new 0 --sizing-capital 100000 >> /app/logs/live_portfolio_manager.log 2>&1"

echo DONE
pause

