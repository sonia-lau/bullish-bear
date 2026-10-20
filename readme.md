# Bullish Bear
🐂 Bullish by instinct. 🐻 Bear by design.

A bot that argues against your stock thesis before you buy.

## Idea: An application evaluates whether a user should purchase a stock

Retail investors decide alone, and with a bias. They rate messages that agree with their view as more valid, and those with stronger bias are more overconfident and trade more. Usually, when they make searches or chat with a LLM agent, they tend to look for bias that matches their initial opinion. Hence, this application is to help user reasons why they want a stock and test the reasons against evidence that they can check. 

User will interact with the app through a GUI that takes in a stock ticker, the sources that it should search or pick from (yahoo news, general sentiment about the company, company operating reports etc), reason for choosing the stock and risk appetite. If the user does not provide why they want to search this company, it will prompt the user if it’s curious or looking for a buy/sell signal. If no reason is given, the app will ask whether they are just curious or looking for a buy/sell signal. It will then return a sourced summary and play devil’s advocate, so the user can go back and forth to strengthen their position. 

## Why it works
First, every claim in the summary will link to its source. Numbers will be automatically checked against SEC filings and marked verified, and news will show its publish date. The user only has to judge what the evidence means and whether it is current, not whether the numbers are right.

Second, the devil’s advocate will be a separate critic agent that weighs risks against the user’s risk appetite and catches errors like wrong fiscal periods. As it did not write the summary and is prompted to find problems, it is less likely to simply agree. Every objection must cite a verified number or a dated article, and the user can accept, rebut or dismiss each one.

Third, the app will remember the user’s reasons, risk appetite and past decisions, asking before it saves and letting the user edit what it keeps. It will also track which types of objections (e.g. valuation) the user keeps dismissing and raise those first, with the reason shown, so their attention goes where bias is most likely. Since a dismissal can also be good judgement, the user can mute a pattern they ignore on purpose, and later filings will show whether a dismissed concern came true.

This will be evaluated with a small sample group to test the functionality, usability and how user-friendly it can be. As this is intended to remove a professional investment broker layer, it needs to have the knowledge whether the user should buy the stock given their risk appetite and market sentiment of the stock. By evaluating in a small group, we can tell whether the app is user friendly, and easy enough for the user to clarify their position for a stock and ability to persuade/dissuade the user from making an expensive mistake. 