import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { requestAPI } from './request';

/**
 * Initialization data for the blaym-jupyter extension.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: 'blaym-jupyter:plugin',
  description: 'History, replay and branching for Jupyter notebooks',
  autoStart: true,
  activate: (app: JupyterFrontEnd) => {
    console.log('JupyterLab extension blaym-jupyter is activated!');

    requestAPI<any>('hello', app.serviceManager.serverSettings)
      .then(data => {
        console.log(data);
      })
      .catch(reason => {
        console.error(
          `The blaym_jupyter server extension appears to be missing.\n${reason}`
        );
      });
  }
};

export default plugin;
