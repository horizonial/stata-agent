import delimited "D:/work/stata agent/app/examples/ck1994/prepared/ck_long.csv", clear

* --- describe data ---
describe
summarize sheet nj wave fte
tab nj wave

* --- create post indicator ---
gen post = (wave==2)
label var post "post (wave==2)"

* --- DiD regression: fte on nj, post, interaction, cluster sheet ---
reg fte i.nj##i.post, cluster(sheet)

* --- core coefficient ---
di "MACHINE_B=" %9.6f _b[1.nj#1.post]
di "MACHINE_SE=" %9.6f _se[1.nj#1.post]
di "MACHINE_N=" e(N)
di "MACHINE_R2=" %9.6f e(r2)
