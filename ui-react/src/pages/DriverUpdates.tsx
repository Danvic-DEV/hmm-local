import React, { useState, useEffect } from 'react';
import { RefreshCw, Download, AlertCircle, CheckCircle, Package } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';

interface DriverInfo {
  name: string;
  driver_type: string;
  category: 'pool' | 'miner' | 'energy' | 'strategy';
  display_name: string;
  current_version: string | null;
  available_version: string;
  status: 'up_to_date' | 'update_available' | 'not_installed';
  description: string | null;
}

interface EnergyProviderInfo {
  name: string;
  provider_id: string;
  display_name: string;
  current_version: string | null;
  available_version: string;
  status: 'up_to_date' | 'update_available' | 'not_installed';
  description: string | null;
}

interface StrategyPluginInfo {
  name: string;
  strategy_id: string;
  display_name: string;
  current_version: string | null;
  available_version: string;
  status: 'up_to_date' | 'update_available' | 'not_installed';
  description: string | null;
}

const DriverUpdates: React.FC = () => {
  const queryClient = useQueryClient();
  const [drivers, setDrivers] = useState<DriverInfo[]>([]);
  const [activeCategory, setActiveCategory] = useState<'all' | 'pool' | 'miner' | 'energy' | 'strategy'>('all');
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState<string | null>(null);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    fetchDriverStatus();
  }, []);

  const fetchDriverStatus = async () => {
    try {
      setLoading(true);
      const [driverResponse, energyProviderResponse, strategyResponse] = await Promise.all([
        fetch('/api/drivers/status'),
        fetch('/api/drivers/energy-providers/status'),
        fetch('/api/drivers/strategies/status')
      ]);

      if (!driverResponse.ok || !energyProviderResponse.ok || !strategyResponse.ok) {
        throw new Error('Failed to fetch driver status');
      }

      const driverData: DriverInfo[] = await driverResponse.json();
      const energyProviderData: EnergyProviderInfo[] = await energyProviderResponse.json();
      const strategyData: StrategyPluginInfo[] = await strategyResponse.json();

      const energyAsDrivers: DriverInfo[] = energyProviderData.map((provider) => ({
        name: provider.name,
        driver_type: provider.provider_id,
        category: 'energy',
        display_name: provider.display_name,
        current_version: provider.current_version,
        available_version: provider.available_version,
        status: provider.status,
        description: provider.description
      }));

      const strategiesAsDrivers: DriverInfo[] = strategyData.map((plugin) => ({
        name: plugin.name,
        driver_type: plugin.strategy_id,
        category: 'strategy',
        display_name: plugin.display_name,
        current_version: plugin.current_version,
        available_version: plugin.available_version,
        status: plugin.status,
        description: plugin.description
      }));

      setDrivers([...driverData, ...energyAsDrivers, ...strategiesAsDrivers]);
    } catch (error) {
      console.error('Error fetching driver status:', error);
      setMessage({ type: 'error', text: 'Failed to load driver information' });
    } finally {
      setLoading(false);
    }
  };

  const restartContainer = async () => {
    if (!confirm('Restart the container now? This will briefly interrupt service.')) {
      return;
    }

    try {
      setRestarting(true);
      const response = await fetch('/api/settings/restart', {
        method: 'POST'
      });

      if (response.ok) {
        setMessage({
          type: 'success',
          text: 'Container restart initiated. Page will reload in 10 seconds...'
        });
        
        // Wait 10 seconds then reload the page
        setTimeout(() => {
          window.location.reload();
        }, 10000);
      } else {
        throw new Error('Restart request failed');
      }
    } catch (error: any) {
      setMessage({
        type: 'error',
        text: `Failed to restart container: ${error.message}`
      });
      setRestarting(false);
    }
  };

  const updateDriver = async (driverName: string, category: 'pool' | 'miner' | 'energy' | 'strategy') => {
    try {
      setUpdating(driverName);
      setMessage(null);

      const endpoint = category === 'energy'
        ? `/api/drivers/energy-providers/update/${driverName}`
        : category === 'strategy'
        ? `/api/drivers/strategies/update/${driverName}`
        : `/api/drivers/update/${category}/${driverName}`;

      const response = await fetch(endpoint, {
        method: 'POST'
      });
      
      const result = await response.json();
      
      if (response.ok) {
        setMessage({
          type: 'success',
          text: `${driverName} updated successfully! Restart required to load new version.`
        });
        setRestartRequired(true);
        await fetchDriverStatus(); // Refresh list
        queryClient.invalidateQueries({ queryKey: ['driver-updates'] }); // Update notification bell
      } else {
        throw new Error(result.detail || 'Update failed');
      }
    } catch (error: any) {
      setMessage({
        type: 'error',
        text: `Failed to update ${driverName}: ${error.message}`
      });
    } finally {
      setUpdating(null);
    }
  };

  const installDriver = async (driverName: string, category: 'pool' | 'miner' | 'energy' | 'strategy') => {
    try {
      setUpdating(driverName);
      setMessage(null);

      const endpoint = category === 'energy'
        ? `/api/drivers/energy-providers/update/${driverName}`
        : category === 'strategy'
        ? `/api/drivers/strategies/update/${driverName}`
        : `/api/drivers/install/${category}/${driverName}`;

      const response = await fetch(endpoint, {
        method: 'POST'
      });
      
      const result = await response.json();
      
      if (response.ok) {
        setMessage({
          type: 'success',
          text: `${driverName} installed successfully! Restart required to load update.`
        });
        setRestartRequired(true);
        await fetchDriverStatus(); // Refresh list
        queryClient.invalidateQueries({ queryKey: ['driver-updates'] }); // Update notification bell
      } else {
        throw new Error(result.detail || 'Installation failed');
      }
    } catch (error: any) {
      setMessage({
        type: 'error',
        text: `Failed to install ${driverName}: ${error.message}`
      });
    } finally {
      setUpdating(null);
    }
  };

  const updateAllDrivers = async () => {
    try {
      setUpdating('all');
      setMessage(null);

      const [driverResponse, energyProviderResponse, strategyResponse] = await Promise.all([
        fetch('/api/drivers/update-all', { method: 'POST' }),
        fetch('/api/drivers/energy-providers/update-all', { method: 'POST' }),
        fetch('/api/drivers/strategies/update-all', { method: 'POST' })
      ]);

      const [driverResult, energyProviderResult, strategyResult] = await Promise.all([
        driverResponse.json(),
        energyProviderResponse.json(),
        strategyResponse.json()
      ]);

      if (driverResponse.ok && energyProviderResponse.ok && strategyResponse.ok) {
        const updateCount = (driverResult.updated?.length || 0) + (energyProviderResult.updated?.length || 0) + (strategyResult.updated?.length || 0);
        const failCount = (driverResult.failed?.length || 0) + (energyProviderResult.failed?.length || 0) + (strategyResult.failed?.length || 0);
        
        if (failCount > 0) {
          setMessage({
            type: 'error',
            text: `Updated ${updateCount} driver(s), but ${failCount} failed. Check logs for details.`
          });
        } else {
          setMessage({
            type: 'success',
            text: `Successfully updated ${updateCount} driver(s)! Restart required.`
          });
        }
        
        setRestartRequired(true);
        await fetchDriverStatus(); // Refresh list
        queryClient.invalidateQueries({ queryKey: ['driver-updates'] }); // Update notification bell
      } else {
        throw new Error(driverResult.detail || energyProviderResult.detail || strategyResult.detail || 'Update all failed');
      }
    } catch (error: any) {
      setMessage({
        type: 'error',
        text: `Failed to update drivers: ${error.message}`
      });
    } finally {
      setUpdating(null);
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'up_to_date':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
            <CheckCircle className="w-3 h-3 mr-1" />
            Up to Date
          </span>
        );
      case 'update_available':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
            <AlertCircle className="w-3 h-3 mr-1" />
            Update Available
          </span>
        );
      case 'not_installed':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800">
            <Package className="w-3 h-3 mr-1" />
            Not Installed
          </span>
        );
      default:
        return null;
    }
  };

  const updatesAvailable = drivers.filter(d => d.status === 'update_available').length;
  const getCategoryCount = (category: 'pool' | 'miner' | 'energy' | 'strategy') => (
    drivers.filter(d => d.category === category).length
  );

  const filteredDrivers = activeCategory === 'all'
    ? drivers
    : drivers.filter(d => d.category === activeCategory);

  const notInstalled = filteredDrivers.filter(d => d.status === 'not_installed');
  const installed = filteredDrivers.filter(d => d.status !== 'not_installed');

  const categoryButtons: Array<{ key: 'all' | 'pool' | 'miner' | 'energy' | 'strategy'; label: string; count: number }> = [
    { key: 'all', label: 'All', count: drivers.length },
    { key: 'pool', label: 'Pool', count: getCategoryCount('pool') },
    { key: 'miner', label: 'Miner', count: getCategoryCount('miner') },
    { key: 'energy', label: 'Energy', count: getCategoryCount('energy') },
    { key: 'strategy', label: 'Strategy', count: getCategoryCount('strategy') },
  ];

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-100">Driver Management</h1>
        <p className="mt-1 text-sm text-gray-400">
          Manage pool, miner, and energy provider updates in one place
        </p>
      </div>

      {/* Message Banner */}
      {message && (
        <div className={`mb-6 p-4 rounded-md ${message.type === 'success' ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
          <div className="flex">
            <div className="flex-shrink-0">
              {message.type === 'success' ? (
                <CheckCircle className="h-5 w-5 text-green-400" />
              ) : (
                <AlertCircle className="h-5 w-5 text-red-400" />
              )}
            </div>
            <div className="ml-3">
              <p className={`text-sm font-medium ${message.type === 'success' ? 'text-green-800' : 'text-red-800'}`}>
                {message.text}
              </p>
            </div>
            <div className="ml-auto pl-3">
              <button
                type="button"
                onClick={() => setMessage(null)}
                className="inline-flex rounded-md p-1.5 hover:bg-opacity-75 focus:outline-none"
              >
                <span className="sr-only">Dismiss</span>
                <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Restart Required Banner */}
      {restartRequired && (
        <div className="mb-6 p-4 rounded-md bg-yellow-900/40 border border-yellow-700">
          <div className="flex items-center justify-between">
            <div className="flex items-center">
              <AlertCircle className="h-5 w-5 text-yellow-400 mr-3" />
              <div>
                <p className="text-sm font-medium text-yellow-200">
                  Container restart required to load updated drivers/providers
                </p>
                <p className="text-xs text-yellow-300 mt-1">
                  You can restart now or manually restart the container later
                </p>
              </div>
            </div>
            <div className="flex items-center space-x-3">
              <button
                onClick={() => setRestartRequired(false)}
                className="text-sm text-gray-400 hover:text-gray-300"
              >
                Ignore
              </button>
              <button
                onClick={restartContainer}
                disabled={restarting}
                className="inline-flex items-center px-4 py-2 border border-transparent rounded-md text-sm font-medium text-white bg-yellow-600 hover:bg-yellow-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-yellow-500 disabled:opacity-50"
              >
                <RefreshCw className={`w-4 h-4 mr-2 ${restarting ? 'animate-spin' : ''}`} />
                {restarting ? 'Restarting...' : 'Restart Now'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Action Bar */}
      <div className="mb-6 flex items-center justify-between bg-gray-900/80 border border-gray-800 p-4 rounded-lg">
        <div className="flex items-center space-x-4">
          <button
            onClick={fetchDriverStatus}
            disabled={loading}
            className="inline-flex items-center px-4 py-2 border border-gray-700 rounded-md text-sm font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
          
          {updatesAvailable > 0 && (
            <button
              onClick={updateAllDrivers}
              disabled={updating !== null}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50"
            >
              <Download className={`w-4 h-4 mr-2 ${updating === 'all' ? 'animate-bounce' : ''}`} />
              Update All ({updatesAvailable})
            </button>
          )}
        </div>
        
        <div className="text-sm text-gray-500">
          {installed.length} installed • {notInstalled.length} available
        </div>
      </div>

      {/* Category Filters */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        {categoryButtons.map((button) => {
          const isActive = activeCategory === button.key;
          return (
            <button
              key={button.key}
              onClick={() => setActiveCategory(button.key)}
              className={`inline-flex items-center px-3 py-1.5 rounded-md text-sm font-medium border transition-colors ${
                isActive
                  ? 'bg-blue-600 border-blue-500 text-white'
                  : 'bg-gray-900/70 border-gray-700 text-gray-300 hover:bg-gray-800'
              }`}
            >
              {button.label}
              <span className={`ml-2 inline-flex items-center justify-center rounded-full px-2 py-0.5 text-xs ${
                isActive ? 'bg-blue-500 text-white' : 'bg-gray-700 text-gray-200'
              }`}>
                {button.count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Installed Drivers Table */}
      {installed.length > 0 && (
        <div className="mb-8">
          <h2 className="text-lg font-semibold text-gray-100 mb-4">Installed Drivers</h2>
          <div className="bg-gray-900/80 border border-gray-800 overflow-hidden rounded-lg">
            <table className="min-w-full divide-y divide-gray-800">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Driver
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Type
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Current Version
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Available Version
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Action
                  </th>
                </tr>
              </thead>
              <tbody className="bg-gray-900/40 divide-y divide-gray-800">
                {installed.map((driver) => (
                  <tr key={`${driver.category}:${driver.name}`}>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div>
                        <div className="text-sm font-medium text-gray-100">
                          {driver.display_name}
                        </div>
                        <div className="text-sm text-gray-400">{driver.name}</div>
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-300">
                      {driver.driver_type}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-200">
                      {driver.current_version || 'unknown'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-200">
                      {driver.available_version}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      {getStatusBadge(driver.status)}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                      {driver.status === 'update_available' ? (
                        <button
                          onClick={() => updateDriver(driver.name, driver.category)}
                          disabled={updating !== null}
                          className="text-blue-600 hover:text-blue-900 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {updating === driver.name ? 'Updating...' : 'Update'}
                        </button>
                      ) : (
                        <span className="text-gray-400">Up to date</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Available Drivers (Not Installed) */}
      {notInstalled.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold text-gray-100 mb-4">Available Drivers</h2>
          <div className="bg-gray-900/80 border border-gray-800 overflow-hidden rounded-lg">
            <table className="min-w-full divide-y divide-gray-800">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Driver
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Type
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Version
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Description
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-400 uppercase tracking-wider">
                    Action
                  </th>
                </tr>
              </thead>
              <tbody className="bg-gray-900/40 divide-y divide-gray-800">
                {notInstalled.map((driver) => (
                  <tr key={`${driver.category}:${driver.name}`}>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div>
                        <div className="text-sm font-medium text-gray-100">
                          {driver.display_name}
                        </div>
                        <div className="text-sm text-gray-400">{driver.name}</div>
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-300">
                      {driver.driver_type}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-200">
                      {driver.available_version}
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-400">
                      {driver.description || 'No description available'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                      <button
                        onClick={() => installDriver(driver.name, driver.category)}
                        disabled={updating !== null}
                        className="inline-flex items-center px-3 py-1 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-green-600 hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-green-500 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {updating === driver.name ? (
                          <>
                            <RefreshCw className="w-4 h-4 mr-1 animate-spin" />
                            Installing...
                          </>
                        ) : (
                          <>
                            <Download className="w-4 h-4 mr-1" />
                            Install
                          </>
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Restart Notice */}
      {(message?.type === 'success') && (
        <div className="mt-6 bg-yellow-50 border-l-4 border-yellow-400 p-4">
          <div className="flex">
            <div className="flex-shrink-0">
              <AlertCircle className="h-5 w-5 text-yellow-400" />
            </div>
            <div className="ml-3">
              <p className="text-sm text-yellow-700">
                <strong>Important:</strong> Container restart required for driver changes to take effect.
                Run: <code className="bg-yellow-100 px-2 py-1 rounded">docker-compose restart</code>
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default DriverUpdates;
