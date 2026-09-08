import delimited "D:/work/stata agent/app/examples/ck1994/prepared/ck_long.csv", clear

gen post = (wave==2)
gen did = nj*post

* 描述统计
sum fte nj post did

* 主回归：DiD，聚类到门店(sheet)
reg fte nj post did, cluster(sheet)
est store m1

* 面板DiD（门店固定效应）
xtset sheet wave
xtreg fte did post, fe cluster(sheet)
est store m2

est table m1 m2, b(%9.3f) se(%9.3f) stats(N r2)
