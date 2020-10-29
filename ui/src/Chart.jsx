import React from 'react';
import { useEffect, useState } from 'react';
import {Client} from 'paho-mqtt';
import {VictoryChart, VictoryLabel, VictoryAxis, VictoryTheme, VictoryLine, VictoryLegend, createContainer, VictoryTooltip} from 'victory';
import moment from 'moment';
import Card from '@material-ui/core/Card';

const colors = {
  "1": "#087e8b",
  "1-A": "#087e8b",
  "1-B": "#08278C",

  "2": "#bfd7ea",
  "2-A": "#bfd7ea",
  "2-B": "#C3BEEA",

  "3": "#ff5a5f",
  "3-A": "#ff5a5f",
  "3-B": "#FF5CCE",
}

function linspace(startValue, stopValue, cardinality) {
  var arr = [];
  var step = (stopValue - startValue) / (cardinality - 1);
  for (var i = 0; i < cardinality; i++) {
    arr.push(startValue + (step * i));
  }
  return arr;
}


function Chart(props) {
    let initialSeriesMap = {}

     for (const [i, v] of props.chartData['series'].entries()) {
          initialSeriesMap[v] = {data: props.chartData['data'][i], name: v, color: colors[v]};
     }

    const [seriesMap, setSeriesMap] = useState(initialSeriesMap);
    const [maxTimestamp, setMaxTimestamp] = useState(parseInt(moment().format('x')));
    const [hiddenSeries, sethiddenSeries] = useState(new Set());

    let names = Object.keys(seriesMap);

    function buildEvents() {
        return names.map((name, idx) => {
          return {
            childName: ['legend'],
            target: ['data', 'labels'],
            eventKey: String(idx),
            eventHandlers: {
              onClick: () => {
                return [
                  {
                    childName: ['line-' + name],
                    target: 'data',
                    eventKey: 'all',
                    mutation: () => {
                      if (!hiddenSeries.delete(name)) {
                        // Was not already hidden => add to set
                        hiddenSeries.add(name);
                      }
                      sethiddenSeries(new Set(hiddenSeries));
                      return null;
                    }
                  }
                ];
              }
            }
          };
        });
      }


    function onConnect() {
      client.subscribe(["morbidostat", "+", "experiment", props.topic].join("/"))
    }

    function onMessageArrived(message) {
      let unit = message.topic.split("/")[1];
      seriesMap[unit].data.push({x: parseInt(moment().format('x')), y: parseFloat(message.payloadString)})
      if (seriesMap[unit].data.length > 1000){
        seriesMap[unit].data.pop()
      }
      setSeriesMap(seriesMap)
      setMaxTimestamp(parseInt(moment().format('x')))
    }

    var client = new Client("192.168.0.22", 9001, "webui-chart" + Math.random());

    // 1. listen for message and update the state
    useEffect(() => {
      client.connect({onSuccess:onConnect});
      client.onMessageArrived = onMessageArrived;
    }, [seriesMap]);


    let minTimestamp = Math.min(...Object.values(seriesMap).map(s => parseInt(s.data[0]['x'])))
    let delta_ts = moment(maxTimestamp, 'x').diff(moment(minTimestamp,'x'), 'hours')
    let axis_display_ts_format = ((delta_ts >= 16)
      ?  ((delta_ts >= 5 * 24) ? 'MMM DD' : 'dd HH:mm') : 'H:mm'
    )
    let tooltip_display_ts_format = ((delta_ts >= 16)
      ?  ((delta_ts >= 5 * 24) ? 'MMM DD HH:mm' : 'dd HH:mm') : 'H:mm'
    )


    const VictoryZoomVoronoiContainer = createContainer("voronoi", "zoom");
    return (
      <Card>
      <VictoryChart
        title={props.title}
        domainPadding={10}
        padding={{left: 70, right:80, bottom: 50, top: 50}}
        width={600}
        height={300}
        events={buildEvents()}
        responsive={false}
        theme={VictoryTheme.material}
        containerComponent={
          <VictoryZoomVoronoiContainer
            labels={(d) => `${moment(d.datum.x, 'x').format(tooltip_display_ts_format)}
${Math.round(d.datum.y * 1000)/1000}`}
            labelComponent={<VictoryTooltip cornerRadius={0} flyoutStyle={{fill: "white", "stroke": "#90a4ae", strokeWidth: 1.5}}/>}
          />
        }
        >
        <VictoryLabel text={props.title} x={300} y={30} textAnchor="middle" style={{fontSize: 15 * props.fontScale}}/>
        <VictoryAxis
          tickFormat={(mt) => mt.format(axis_display_ts_format)}
          tickValues={linspace(minTimestamp, maxTimestamp + 100000, 6).map(x => moment(x, 'x').startOf(((delta_ts >= 16) ? 'hour' : 'minute')))}
          style={{
            tickLabels: {fontSize: 13 * props.fontScale, padding: 5}
          }}
          offsetY={50}
          orientation="bottom"
        />
        <VictoryAxis
          crossAxis={false}
          dependentAxis
          label={props.yAxisLabel}
          axisLabelComponent={<VictoryLabel  dy={-40} style={{fontSize: 15 * props.fontScale, padding:10}}/>}
          style={{
            tickLabels: {fontSize: 13 * props.fontScale, padding: 5}
          }}
        />
        <VictoryLegend x={527} y={60}
          name={'legend'}
          borderPadding={{right: 8}}
          orientation="vertical"
          cursor = {"pointer"}
          style={{
            border: { stroke: "#90a4ae" },
            labels: { fontSize: 13 * props.fontScale },
            data: { stroke: "black", strokeWidth: 1 },
          }}
          data={names.map(name => {
            const line = seriesMap[name]
            const item = {name: line.name, symbol: {fill: line.color,  type: "square"}};
            if (hiddenSeries.has(name)) {
              return { ...item, symbol: { fill: 'white',  type: "square"} };
            }
            return item;
          })}
        />
      {
        Object.keys(seriesMap).map(name => {
          if (hiddenSeries.has(name)) {
              return undefined;
          }
          return <VictoryLine
            interpolation={props.interpolation}
            key={'line-' + name}
            name={'line-' + name}
            style={{
              data: { stroke: seriesMap[name].color, strokeWidth: 2 },
              parent: { border: "1px solid #ccc"}
            }}
            data={seriesMap[name].data}
            x="x"
            y="y"
          />
      })}
    </VictoryChart>
    </Card>
    )
}

export default Chart;
