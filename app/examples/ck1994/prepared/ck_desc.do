import delimited "D:/work/stata agent/app/examples/ck1994/prepared/ck_long.csv", clear
log using "D:/work/stata agent/app/examples/ck1994/prepared/ck_desc.log", replace text
describe
summarize sheet nj wave fte
tab nj wave
log close
