import React from 'react'
import {makeStyles} from '@material-ui/styles';
import Card from '@material-ui/core/Card';
import CardContent from '@material-ui/core/Card';
import {Typography} from '@material-ui/core';

const useStyles = makeStyles({
  root: {
    minWidth: 100,
    marginTop: "15px"
  },
  title: {
    fontSize: 14,
  },
  cardContent: {
    padding: "10px"
  },
  pos: {
    marginBottom: 0,
  },
});

function ExperimentSummary(props){
  const classes = useStyles();

  return(
    <Card className={classes.root}>
      <CardContent className={classes.cardContent}>
        <Typography className={classes.title} color="textSecondary" gutterBottom>
          Experiment
        </Typography>
        <Typography variant="h5" component="h2">
          Trial 14
        </Typography>
        <Typography variant="body2" component="p">
          This is the description of the experiment. This description is stored in a database, along with
          the other metadata in the experiment, like <code>started date</code>.
        </Typography>
      </CardContent>
    </Card>
  )
}


export default ExperimentSummary;