import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputPath = "D:/YuGong/Chuangdai_Project/ZhiLeng_ChuangDai_YiFu/YaSuoJi_Plan/YaSuoJi_Cat1_POC_Plan/JingWuPOC/SN_SIM对应表.xlsx";
const previewPath = "D:/YuGong/Chuangdai_Project/ZhiLeng_ChuangDai_YiFu/YaSuoJi_Plan/YaSuoJi_Cat1_POC_Plan/JingWuPOC/.codex_tmp/SN_SIM对应表_preview.png";

const records = [
  [1, 1, "YNPOC110160930100001", "89861590112540036908"],
  [2, 2, "YNPOC110160930100002", "89861590112540036909"],
  [3, 3, "YNPOC110160930100003", "89861590112540036910"],
  [4, 4, "YNPOC110160930100004", "89861590112540036911"],
  [5, 5, "YNPOC110160930100005", "89861590112540036912"],
  [6, 6, "YNPOC110160930100006", "89861590112540036913"],
  [7, 7, "YNPOC110160930100007", "89861590112540036914"],
  [8, 8, "YNPOC110160930100008", "89861590112540036915"],
  [9, 9, "YNPOC110160930100009", "89861590112540036916"],
  [10, 10, "YNPOC110160930100010", "89861590112540036917"],
  [11, 11, "YNPOC110160930100011", "89861590112540036918"],
  [12, 12, "YNPOC110160930100012", "89861590112540036919"],
  [13, 13, "YNPOC110160930100013", "89861590112540036920"],
  [14, 14, "YNPOC110160930100014", "89861590112540036921"],
  [15, 15, "YNPOC110160930100015", "89861590112540036922"],
  [16, 16, "YNPOC110160930100016", "89861590112540036923"],
  [17, 17, "YNPOC110160930100017", "89861590112540036925"],
  [18, 18, "YNPOC110160930100018", "89861590112540036925"],
  [19, 19, "YNPOC110160930100019", "89861590112540036926"],
  [20, 20, "YNPOC110160930100020", "89861590112540036927"],
];

function groupIccid(value) {
  return value.match(/.{1,4}/g).join(" ");
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("SN与SIM清单");
sheet.showGridLines = false;
sheet.tabColor = "#1F4E78";

sheet.getRange("A1:D21").values = [
  ["序号", "设备序列号", "完整SN", "SIM卡号（ICCID）"],
  ...records.map(([index, serial, sn, iccid]) => [
    index,
    serial,
    sn,
    groupIccid(iccid),
  ]),
];

const body = sheet.getRange("A1:D21");
body.format.font = { name: "Arial", size: 11, color: "#1F2937" };
body.format.verticalAlignment = "center";

const header = sheet.getRange("A1:D1");
header.format = {
  fill: "#17365D",
  font: { name: "Arial", size: 11, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: false,
  borders: { preset: "all", style: "thin", color: "#D9E2F3" },
};

const data = sheet.getRange("A2:D21");
data.format.borders = {
  insideHorizontal: { style: "thin", color: "#D9E2F3" },
  top: { style: "thin", color: "#D9E2F3" },
  bottom: { style: "thin", color: "#D9E2F3" },
};

// Match the alternating blue/white rows in the supplied reference image.
for (let row = 2; row <= 21; row += 1) {
  if (row % 2 === 0) {
    sheet.getRange(`A${row}:D${row}`).format.fill = "#EAF2F8";
  }
}

sheet.getRange("A2:B21").format.horizontalAlignment = "center";
sheet.getRange("C2:D21").format.horizontalAlignment = "left";
sheet.getRange("B2:B21").format.numberFormat = "00000";
sheet.getRange("C2:D21").format.numberFormat = "@";

sheet.getRange("A1:A21").format.columnWidth = 9;
sheet.getRange("B1:B21").format.columnWidth = 14;
sheet.getRange("C1:C21").format.columnWidth = 28;
sheet.getRange("D1:D21").format.columnWidth = 28;
sheet.getRange("A1:D1").format.rowHeight = 26;
sheet.getRange("A2:D21").format.rowHeight = 22;
sheet.freezePanes.freezeRows(1);

workbook.recalculate();

const check = await workbook.inspect({
  kind: "table",
  range: "SN与SIM清单!A1:D21",
  include: "values,formulas",
  tableMaxRows: 25,
  tableMaxCols: 4,
});
console.log(check.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "SN与SIM清单",
  range: "A1:D21",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`saved=${outputPath}`);
