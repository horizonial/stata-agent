import delimited "D:/work/stata agent/app/examples/ck1994/prepared/ck_long.csv", clear
gen post = (wave==2)
reg fte i.nj##i.post, vce(cluster sheet)
di "MACHINE_B=" %9.6f _b[1.nj#1.post]
di "MACHINE_SE=" %9.6f _se[1.nj#1.post]
di "MACHINE_N=" e(N)
di "MACHINE_R2=" %9.6f e(r2)
di "STA_ENV version=" c(version)
di "STA_ENV flavor=" c(flavor)