For this demo, the pdf for scraping is nanosized_powders.pdf

As a very general overview, the paper discusses TEM, SAXS, and XRD methods for determining size distributions. However, we are only interested in XRD results.
This pdf parsing scripts demonstrates that it only parses figures that are related to XRD, as shown in the demo figures folder (figures 1, 5)

<img width="1542" height="815" alt="fig_1" src="https://github.com/user-attachments/assets/a09a3a32-4c04-430a-b49e-e86db96ba14d" />
<img width="1542" height="1681" alt="fig_5" src="https://github.com/user-attachments/assets/f8afa84b-f1aa-4282-a777-2fe25fa71501" />

Note that it will parse all figures if ran without the --xrd-figures-only tag

In addition, the script will output an organized json file that contains, for each figure, a path, caption, and relevant context. (nanosized_powders/nanosized_powders.figure_analysis.json)
