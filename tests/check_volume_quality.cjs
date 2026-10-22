const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../frontend/js/04-subchart.js'),'utf8');
const code=source.slice(source.indexOf('        function buildValueMap('),source.indexOf('        function getSubIndicatorTitle('));
const bars=Array.from({length:30},(_,i)=>({bar_time:`2026-10-21T10:${String(i).padStart(2,'0')}:00`,
  open_price:'100',high_price:'101',low_price:'99',close_price:'100',volume:100,open_interest:1000}));
bars[10].volume=190474; // Large, known values must never be clipped by appearance.
bars[11].volume=999999;bars[11].provenance_json={volume_quality:'UNVERIFIED_ALLOCATION'};
bars[12].volume=null;
let points;
const series={setData(data){points=data;},priceScale(){return {applyOptions(){}};}};
const env={subChart:{removeSeries(){},addHistogramSeries(){return series;}},subSeriesList:[],subValueMap:{},
 currentPeriod:'1m',barsData:bars,intradayBars:[],subIndicator:'VOL',updateSubIndicatorLegend(){}};
vm.runInNewContext(code,env);env.renderSubChart();
assert.equal(points[10].value,190474,'keep real large columns at their original height');
assert.equal('value' in points[11],false,'quarantined raw values must become gaps');
assert.equal('value' in points[12],false,'unknown is not zero');
assert.equal(env.subValueMap[points[11].time],undefined,'cursor must not use the bad raw number');
console.log('Volume quality: actual heights retained; unknown columns and cursor values excluded');
