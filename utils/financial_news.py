'''
TODO: 
'''

from datetime import date
from dateutil.relativedelta import relativedelta

import finnhub

from CONSTANTS import FINNHUB_API_KEY

finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)

def find_company(string): 
    result = finnhub_client.symbol_lookup(string)
    ticker = result["result"][0]["displaySymbol"]
    return ticker

def get_company_news(ticker, start_date=None, end_date=None): 
    if start_date is None: 
        start_date = str(date.today() - relativedelta(days=10))

    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.company_news(ticker, _from=start_date, to=end_date)

# def get_company_peers(ticker, grouping="subIndustry"): 
#     if grouping not in ["sector", "industry", "subIndustry"]: 
#         raise KeyError("Grouping not in ['sector', 'industry', 'subIndustry']")
#     return finnhub_client.company_peers(ticker)

def get_company_basic_financials(ticker, metric="all"): 
    return finnhub_client.company_basic_financials(ticker, metric)

def get_insider_sentiment(ticker, start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.stock_insider_transactions(ticker, start_date, end_date)

def get_finance_reports(ticker, freq="quarterly", start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.financials_reported(symbol=ticker, freq=freq, _from=start_date, to=end_date)

def get_sec_filings(ticker, start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.filings(ticker, _from=start_date, to=end_date)

def get_reccommendation_trends(ticker): 
    return finnhub_client.recommendation_trends(ticker)

def get_earning_calendar(ticker, start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.earnings_calendar(symbol=ticker, _from=start_date, to=end_date)

def get_quote(ticker): 
     return finnhub_client.quote(ticker)

def get_senate_lobbying(ticker, start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.stock_lobbying(symbol=ticker, _from=start_date, to=end_date)

def get_govt_spending(ticker, start_date=None, end_date=None): 
    if start_date is None: 
            start_date = str(date.today() - relativedelta(months=1))
    if end_date is None: 
        end_date = str(date.today())
    return finnhub_client.stock_usa_spending(symbol=ticker, _from=start_date, to=end_date)

# print("---find_company---\n", find_company("SEAGATE"), "\n")
# print("---get_company_news---\n", get_company_news("STX"), "\n")
# print("---get_company_basic_financials---\n", get_company_basic_financials("STX"), "\n")
# print("---get_insider_sentiment---\n", get_insider_sentiment("STX"), "\n")
# print("---get_finance_reports---\n", get_finance_reports("STX"), "\n")
# print("---get_sec_filings---\n", get_sec_filings("STX"), "\n")
# print("---get_reccommendation_trends---\n", get_reccommendation_trends("STX"), "\n")
# print("---get_earning_calendar---\n", get_earning_calendar("STX"), "\n")
# print("---get_quote---\n", get_quote("STX"), "\n")
# print("---get_senate_lobbying---\n", get_senate_lobbying("STX"), "\n")
# print("---get_govt_spending---\n", get_govt_spending("STX"), "\n")